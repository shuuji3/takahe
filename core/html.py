import bleach
from bleach.linkifier import LinkifyFilter
from django.utils.safestring import mark_safe


def allow_a(tag: str, name: str, value: str):
    if name in ["href", "title", "class"]:
        return True
    elif name == "rel":
        # Only allow rel attributes with a small subset of values
        # (we're defending against, for example, rel=me)
        rel_values = value.split()
        if all(v in ["nofollow", "noopener", "noreferrer", "tag"] for v in rel_values):
            return True
    return False


def sanitize_html(post_html: str) -> str:
    """
    Only allows a, br, p and span tags, and class attributes.
    """
    cleaner = bleach.Cleaner(
        tags=["br", "p"],
        attributes={  # type:ignore
            "a": allow_a,
            "p": ["class"],
            "span": ["class"],
        },
        filters=[LinkifyFilter],
        strip=True,
    )
    return mark_safe(cleaner.clean(post_html))


def strip_html(post_html: str) -> str:
    """
    Strips all tags from the text, then linkifies it.
    """
    cleaner = bleach.Cleaner(tags=[], strip=True, filters=[LinkifyFilter])
    return mark_safe(cleaner.clean(post_html))


def html_to_plaintext(post_html: str) -> str:
    """
    Tries to do the inverse of the linebreaks filter.
    """
    # TODO: Handle HTML entities
    # Remove all newlines, then replace br with a newline and /p with two (one comes from bleach)
    post_html = post_html.replace("\n", "").replace("<br>", "\n").replace("</p>", "\n")
    # Remove all other HTML and return
    cleaner = bleach.Cleaner(tags=[], strip=True, filters=[])
    return cleaner.clean(post_html).strip()


class ContentRenderer:
    """
    Renders HTML for posts, identity fields, and more.

    The `local` parameter affects whether links are absolute (False) or relative (True)
    """

    def __init__(self, local: bool):
        self.local = local

    def render_post(self, html: str, post) -> str:
        """
        Given post HTML, normalises it and renders it for presentation.
        """
        if not html:
            return ""
        html = sanitize_html(html)
        html = self.linkify_mentions(html, post=post)
        html = self.linkify_hashtags(html, identity=post.author)
        if self.local:
            html = self.imageify_emojis(html, identity=post.author)
        return mark_safe(html)

    def render_identity(self, html: str, identity, strip: bool = False) -> str:
        """
        Given identity field HTML, normalises it and renders it for presentation.
        """
        if not html:
            return ""
        if strip:
            html = strip_html(html)
        else:
            html = sanitize_html(html)
        html = self.linkify_hashtags(html, identity=identity)
        if self.local:
            html = self.imageify_emojis(html, identity=identity)
        return mark_safe(html)

    def linkify_mentions(self, html: str, post) -> str:
        """
        Links mentions _in the context of the post_ - as in, using the mentions
        property as the only source (as we might be doing this without other
        DB access allowed)
        """
        from activities.models import Post

        possible_matches = {}
        for mention in post.mentions.all():
            if self.local:
                url = str(mention.urls.view)
            else:
                url = mention.absolute_profile_uri()
            possible_matches[mention.username] = url
            possible_matches[f"{mention.username}@{mention.domain_id}"] = url

        collapse_name: dict[str, str] = {}

        def replacer(match):
            precursor = match.group(1)
            handle = match.group(2).lower()
            if "@" in handle:
                short_handle = handle.split("@", 1)[0]
            else:
                short_handle = handle
            if handle in possible_matches:
                if short_handle not in collapse_name:
                    collapse_name[short_handle] = handle
                elif collapse_name.get(short_handle) != handle:
                    short_handle = handle
                return f'{precursor}<a href="{possible_matches[handle]}">@{short_handle}</a>'
            else:
                return match.group()

        return Post.mention_regex.sub(replacer, html)

    def linkify_hashtags(self, html, identity) -> str:
        from activities.models import Hashtag

        def replacer(match):
            hashtag = match.group(1)
            if self.local:
                return (
                    f'<a class="hashtag" href="/tags/{hashtag.lower()}/">#{hashtag}</a>'
                )
            else:
                return f'<a class="hashtag" href="https://{identity.domain.uri_domain}/tags/{hashtag.lower()}/">#{hashtag}</a>'

        return Hashtag.hashtag_regex.sub(replacer, html)

    def imageify_emojis(self, html: str, identity, include_local: bool = True):
        """
        Find :emoji: in content and convert to <img>. If include_local is True,
        the local emoji will be used as a fallback for any shortcodes not defined
        by emojis.
        """
        from activities.models import Emoji

        emoji_set = Emoji.for_domain(identity.domain)
        if include_local:
            emoji_set.extend(Emoji.for_domain(None))

        possible_matches = {
            emoji.shortcode: emoji.as_html() for emoji in emoji_set if emoji.is_usable
        }

        def replacer(match):
            fullcode = match.group(1).lower()
            if fullcode in possible_matches:
                return possible_matches[fullcode]
            return match.group()

        return Emoji.emoji_regex.sub(replacer, html)
