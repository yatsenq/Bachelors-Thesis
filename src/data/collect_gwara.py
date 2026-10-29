"""Збір відкритого WordPress API Ґвари; при HTTP 403 збір зупиняється."""
from factcheck_common import run

CONFIG = {
    'source': 'gwara', 'name': 'Ґвара Медіа', 'base': 'https://gwaramedia.com',
    'sources': ['factcheck', 'news'],
    'factcheck_slugs': {'factcheck', 'fact-check', 'faktchek', 'fakty', 'perevirka'},
    'news_slugs': {'news', 'novyny', 'novini'},
}

if __name__ == '__main__':
    run(CONFIG)
