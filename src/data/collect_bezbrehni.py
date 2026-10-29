"""Збір фактчеків «Без Брехні»: false, true, manipulation та невизначені."""
from factcheck_common import run

CONFIG = {
    'source': 'bezbrehni', 'name': 'Без Брехні', 'base': 'https://without-lie.info',
    'sources': ['factcheck'],
    'factcheck_slugs': {'factcheck', 'faktchek-news', 'propaganda-stop', 'covid-19',
                        'promify', 'faktchek-rehion', 'infographics', 'ternopillia'},
    'news_slugs': set(),
}

if __name__ == '__main__':
    run(CONFIG)
