from django.db import models, transaction
from django.utils import timezone

from activities.models.fan_out import FanOut
from activities.models.post import Post
from activities.models.timeline_event import TimelineEvent
from core.ld import format_ld_date, parse_ld_date
from stator.models import State, StateField, StateGraph, StatorModel
from users.models.follow import Follow
from users.models.identity import Identity


class PostInteractionStates(StateGraph):
    new = State(try_interval=300)
    fanned_out = State(externally_progressed=True)
    undone = State(try_interval=300)
    undone_fanned_out = State()

    new.transitions_to(fanned_out)
    fanned_out.transitions_to(undone)
    undone.transitions_to(undone_fanned_out)

    @classmethod
    async def handle_new(cls, instance: "PostInteraction"):
        """
        Creates all needed fan-out objects for a new PostInteraction.
        """
        interaction = await instance.afetch_full()
        # Boost: send a copy to all people who follow this user
        if interaction.type == interaction.Types.boost:
            async for follow in interaction.identity.inbound_follows.select_related(
                "source", "target"
            ):
                if follow.source.local or follow.target.local:
                    await FanOut.objects.acreate(
                        type=FanOut.Types.interaction,
                        identity_id=follow.source_id,
                        subject_post=interaction.post,
                        subject_post_interaction=interaction,
                    )
            # And one to the post's author
            await FanOut.objects.acreate(
                type=FanOut.Types.interaction,
                identity_id=interaction.post.author_id,
                subject_post=interaction.post,
                subject_post_interaction=interaction,
            )
        # Like: send a copy to the original post author only
        elif interaction.type == interaction.Types.like:
            await FanOut.objects.acreate(
                type=FanOut.Types.interaction,
                identity_id=interaction.post.author_id,
                subject_post=interaction.post,
                subject_post_interaction=interaction,
            )
        else:
            raise ValueError("Cannot fan out unknown type")
        # And one for themselves if they're local and it's a boost
        if (
            interaction.type == PostInteraction.Types.boost
            and interaction.identity.local
        ):
            await FanOut.objects.acreate(
                identity_id=interaction.identity_id,
                type=FanOut.Types.interaction,
                subject_post=interaction.post,
                subject_post_interaction=interaction,
            )
        return cls.fanned_out

    @classmethod
    async def handle_undone(cls, instance: "PostInteraction"):
        """
        Creates all needed fan-out objects to undo a PostInteraction.
        """
        interaction = await instance.afetch_full()
        # Undo Boost: send a copy to all people who follow this user
        if interaction.type == interaction.Types.boost:
            async for follow in interaction.identity.inbound_follows.select_related(
                "source", "target"
            ):
                if follow.source.local or follow.target.local:
                    await FanOut.objects.acreate(
                        type=FanOut.Types.undo_interaction,
                        identity_id=follow.source_id,
                        subject_post=interaction.post,
                        subject_post_interaction=interaction,
                    )
        # Undo Like: send a copy to the original post author only
        elif interaction.type == interaction.Types.like:
            await FanOut.objects.acreate(
                type=FanOut.Types.undo_interaction,
                identity_id=interaction.post.author_id,
                subject_post=interaction.post,
                subject_post_interaction=interaction,
            )
        else:
            raise ValueError("Cannot fan out unknown type")
        # And one for themselves if they're local and it's a boost
        if (
            interaction.type == PostInteraction.Types.boost
            and interaction.identity.local
        ):
            await FanOut.objects.acreate(
                identity_id=interaction.identity_id,
                type=FanOut.Types.undo_interaction,
                subject_post=interaction.post,
                subject_post_interaction=interaction,
            )
        return cls.undone_fanned_out


class PostInteraction(StatorModel):
    """
    Handles both boosts and likes
    """

    class Types(models.TextChoices):
        like = "like"
        boost = "boost"

    # The state the boost is in
    state = StateField(PostInteractionStates)

    # The canonical object ID
    object_uri = models.CharField(max_length=500, blank=True, null=True, unique=True)

    # What type of interaction it is
    type = models.CharField(max_length=100, choices=Types.choices)

    # The user who boosted/liked/etc.
    identity = models.ForeignKey(
        "users.Identity",
        on_delete=models.CASCADE,
        related_name="interactions",
    )

    # The post that was boosted/liked/etc
    post = models.ForeignKey(
        "activities.Post",
        on_delete=models.CASCADE,
        related_name="interactions",
    )

    # When the activity was originally created (as opposed to when we received it)
    # Mastodon only seems to send this for boosts, not likes
    published = models.DateTimeField(default=timezone.now)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        index_together = [["type", "identity", "post"]]

    ### Display helpers ###

    @classmethod
    def get_post_interactions(cls, posts, identity):
        """
        Returns a dict of {interaction_type: set(post_ids)} for all the posts
        and the given identity, for use in templates.
        """
        # Bulk-fetch any interactions
        ids_with_interaction_type = cls.objects.filter(
            identity=identity,
            post_id__in=[post.pk for post in posts],
            type__in=[cls.Types.like, cls.Types.boost],
            state__in=[PostInteractionStates.new, PostInteractionStates.fanned_out],
        ).values_list("post_id", "type")
        # Make it into the return dict
        result = {}
        for post_id, interaction_type in ids_with_interaction_type:
            result.setdefault(interaction_type, set()).add(post_id)
        return result

    @classmethod
    def get_event_interactions(cls, events, identity):
        """
        Returns a dict of {interaction_type: set(post_ids)} for all the posts
        within the events and the given identity, for use in templates.
        """
        return cls.get_post_interactions(
            [e.subject_post for e in events if e.subject_post], identity
        )

    ### Async helpers ###

    async def afetch_full(self):
        """
        Returns a version of the object with all relations pre-loaded
        """
        return await PostInteraction.objects.select_related("identity", "post").aget(
            pk=self.pk
        )

    ### ActivityPub (outbound) ###

    def to_ap(self) -> dict:
        """
        Returns the AP JSON for this object
        """
        # Create an object URI if we don't have one
        if self.object_uri is None:
            self.object_uri = self.identity.actor_uri + f"#{self.type}/{self.id}"
        if self.type == self.Types.boost:
            value = {
                "type": "Announce",
                "id": self.object_uri,
                "published": format_ld_date(self.published),
                "actor": self.identity.actor_uri,
                "object": self.post.object_uri,
                "to": "as:Public",
            }
        elif self.type == self.Types.like:
            value = {
                "type": "Like",
                "id": self.object_uri,
                "published": format_ld_date(self.published),
                "actor": self.identity.actor_uri,
                "object": self.post.object_uri,
            }
        else:
            raise ValueError("Cannot turn into AP")
        return value

    def to_undo_ap(self) -> dict:
        """
        Returns the AP JSON to undo this object
        """
        object = self.to_ap()
        return {
            "id": object["id"] + "/undo",
            "type": "Undo",
            "actor": self.identity.actor_uri,
            "object": object,
        }

    ### ActivityPub (inbound) ###

    @classmethod
    def by_ap(cls, data, create=False) -> "PostInteraction":
        """
        Retrieves a PostInteraction instance by its ActivityPub JSON object.

        Optionally creates one if it's not present.
        Raises KeyError if it's not found and create is False.
        """
        # Do we have one with the right ID?
        try:
            boost = cls.objects.get(object_uri=data["id"])
        except cls.DoesNotExist:
            if create:
                # Resolve the author
                identity = Identity.by_actor_uri(data["actor"], create=True)
                # Resolve the post
                post = Post.by_object_uri(data["object"], fetch=True)
                # Get the right type
                if data["type"].lower() == "like":
                    type = cls.Types.like
                elif data["type"].lower() == "announce":
                    type = cls.Types.boost
                else:
                    raise ValueError(f"Cannot handle AP type {data['type']}")
                # Make the actual interaction
                boost = cls.objects.create(
                    object_uri=data["id"],
                    identity=identity,
                    post=post,
                    published=parse_ld_date(data.get("published", None))
                    or timezone.now(),
                    type=type,
                )
            else:
                raise cls.DoesNotExist(f"No interaction with ID {data['id']}", data)
        return boost

    @classmethod
    def handle_ap(cls, data):
        """
        Handles an incoming announce/like
        """
        with transaction.atomic():
            # Create it
            try:
                interaction = cls.by_ap(data, create=True)
            except (cls.DoesNotExist, Post.DoesNotExist):
                # That post is gone, boss
                # TODO: Limited retry state?
                return
            # Boosts (announces) go to everyone who follows locally
            if interaction.type == cls.Types.boost:
                for follow in Follow.objects.filter(
                    target=interaction.identity, source__local=True
                ):
                    TimelineEvent.add_post_interaction(follow.source, interaction)
            # Likes go to just the author of the post
            elif interaction.type == cls.Types.like:
                TimelineEvent.add_post_interaction(interaction.post.author, interaction)
            # Force it into fanned_out as it's not ours
            interaction.transition_perform(PostInteractionStates.fanned_out)

    @classmethod
    def handle_undo_ap(cls, data):
        """
        Handles an incoming undo for a announce/like
        """
        with transaction.atomic():
            # Find it
            try:
                interaction = cls.by_ap(data["object"])
            except (cls.DoesNotExist, Post.DoesNotExist):
                # Well I guess we don't need to undo it do we
                return
            # Verify the actor matches
            if data["actor"] != interaction.identity.actor_uri:
                raise ValueError("Actor mismatch on interaction undo")
            # Delete all events that reference it
            interaction.timeline_events.all().delete()
            # Force it into undone_fanned_out as it's not ours
            interaction.transition_perform(PostInteractionStates.undone_fanned_out)
