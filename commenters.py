#!/usr/bin/env python3
"""Who commented a keyword on one of our reels. Read-only: it never replies or messages.

    python3 commenters.py <project id | media id> [keyword]   # tab-separated, oldest first

Columns: comment id, when, @user, answered (we replied under it), text. Meta leaves
the names out unless the saved token has instagram_manage_comments.
"""
import sys
from pathlib import Path
import publish as pub


def main(ref: str, word: str = "") -> None:
    media = ref if ref.isdigit() else pub._rec(Path(pub._row(ref)["dir"])).get("media_id")
    if not media:
        sys.exit(f"{ref}: not published from here, pass the media id")
    me, n = pub.token(), 0
    for c in sorted(pub.comments(media), key=lambda c: c["timestamp"]):
        if word.lower() not in c.get("text", "").lower() or not pub.waiting([{**c, "replies": {}}], me):
            continue   # not the keyword, or our own comment
        n += 1
        print(c["id"], c["timestamp"][:16], "@" + c.get("username", "?"), "-" if pub.waiting([c], me) else "answered",
              c["text"].replace("\n", " "), sep="\t")
    print(f"{n} comments", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
