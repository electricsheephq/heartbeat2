"""The plugin catalog renders the README with raw HTML dropped, so the demo screenshots must be markdown images."""
import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / 'README.md'


def test_see_it_work_screenshots_are_markdown_images():
    # Fenced code renders as text, so an image reference inside a ``` or ~~~ block does not count.
    # The closing fence must repeat the opening one, so a ``` line inside a ```` block does not end it.
    # Fences may be indented (up to three spaces in CommonMark, more inside list items), so allow leading spaces.
    text = re.sub(r'^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1', '', README.read_text(), flags=re.S | re.M)
    for name in ('see-it-work-1-armed.png', 'see-it-work-2-followup.png', 'see-it-work-3-fix.png',
                 'see-it-work-4-roles.png', 'see-it-work-5-desk.png'):
        assert re.search(r'!\[[^\]]+\]\(docs/media/' + re.escape(name) + r'\)', text), name
        assert not re.search(r'<img[^>]+' + re.escape(name), text), name
