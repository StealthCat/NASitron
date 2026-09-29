from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import pytest


def test_all_templates_parse():
    template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    templates = sorted(p.name for p in template_dir.glob("*.html"))
    assert templates
    for name in templates:
        source, _, _ = env.loader.get_source(env, name)
        env.parse(source)


@pytest.mark.parametrize(
    ("pools", "expected"),
    [
        # Weight allocation by pool size: an average of percentages would be 50%.
        ([{"size_bytes": 100, "alloc_bytes": 90}, {"size_bytes": 900, "alloc_bytes": 90}], "18% used"),
        # Exclude a pool whose allocation was not reported.
        ([{"size_bytes": 100, "alloc_bytes": 90}, {"size_bytes": 900}], "90% used"),
        ([{"size_bytes": 100, "alloc_bytes": 0}], "0% used"),
        ([{"size_bytes": 100, "alloc_bytes": 150}], "100% used"),
        ([{"size_bytes": 0, "alloc_bytes": 0}], None),
        ([], None),
    ],
)
def test_server_capacity_uses_reported_allocation(pools, expected):
    from app.main import templates

    components = templates.env.get_template("components.html").make_module()
    rendered = components.server_capacity({"pools": pools})
    if expected is None:
        assert "server-capacity" not in rendered
    else:
        assert expected in rendered
