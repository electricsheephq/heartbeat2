"""The plugin catalog renders the README with raw HTML dropped, so the demo screenshots must be markdown images."""
import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / 'README.md'


def test_see_it_work_screenshots_are_markdown_images():
    text = re.sub(r'```.*?```', '', README.read_text(), flags=re.S)
    for name in ('see-it-work-1-armed.png', 'see-it-work-2-followup.png', 'see-it-work-3-fix.png'):
        assert re.search(r'!\[[^\]]+\]\(docs/media/' + re.escape(name) + r'\)', text), name
        assert not re.search(r'<img[^>]+' + re.escape(name), text), name
