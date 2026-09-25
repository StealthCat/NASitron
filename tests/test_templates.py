from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def test_all_templates_parse():
    template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    templates = sorted(p.name for p in template_dir.glob("*.html"))
    assert templates
    for name in templates:
        source, _, _ = env.loader.get_source(env, name)
        env.parse(source)
