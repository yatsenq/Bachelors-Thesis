"""Укрінформ: україномовні новини з міткою true за джерелом (weak label).

Залежності: requests beautifulsoup4 lingua-language-detector.
Тест: python src/data/collect_ukrinform.py --max-sitemaps 1 --max-articles 10
Збір: python src/data/collect_ukrinform.py --max-articles 0 --per-category 1500
За замовчуванням: 10 спроб завантаження, до 1500 записів на рубрику,
усі доступні в зафіксованій карті дати. --per-category 0 скасовує квоту.
--date-from / --date-to: YYYY-MM-DD. --categories: коди рубрик через пробіл.
Не запускайте два процеси з однаковою папкою --output одночасно.
"""

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = 'https://www.ukrinform.ua'
AGENT = 'BachelorThesisResearch/1.0'
RUBRICS = {
    'ato': 'Війна', 'polytics': 'Політика', 'economy': 'Економіка',
    'world': 'Світ', 'society': 'Суспільство', 'regions': 'Регіони',
    'culture': 'Культура', 'sports': 'Спорт', 'diaspora': 'Діаспора',
    'vidbudova': 'Відбудова',
}
DEFAULT_CATEGORIES = ['ato', 'polytics', 'economy', 'world', 'society', 'regions', 'culture', 'sports']
ARTICLE_PATH = re.compile(r'^/rubric-([a-z]+)/([0-9]+)-[^/]+\.html$')
NS = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9',
      'n': 'http://www.google.com/schemas/sitemap-news/0.9'}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical(url):
    parsed = urlsplit(urljoin(BASE, url))
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('ukrinform.ua', 'www.ukrinform.ua'):
        raise ValueError('Адреса поза українським сайтом Укрінформу: ' + url)
    return urlunsplit(('https', 'www.ukrinform.ua', parsed.path or '/', '', ''))


class SkipArticle(ValueError):
    pass


class Client:
    def __init__(self, delay):
        self.delay = max(1.0, delay)
        self.last = 0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'uk'})
        self.robots = None
        response = self.get(BASE + '/robots.txt')
        if '<html' in response.text.lower() or not re.search(r'(?im)^user-agent\s*:', response.text):
            raise ValueError('Замість robots.txt отримано неочікувану сторінку')
        self.robots = RobotFileParser()
        self.robots.parse(response.text.splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(AGENT) or 0)
        rate = self.robots.request_rate(AGENT)
        if rate and rate.requests:
            self.delay = max(self.delay, rate.seconds / rate.requests)

    def get(self, url):
        url = canonical(url)
        if self.robots and not self.robots.can_fetch(AGENT, url):
            raise PermissionError('robots.txt забороняє адресу: ' + url)
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                response = self.session.get(url, timeout=(15, 45), allow_redirects=False)
                if 300 <= response.status_code < 400:
                    raise ValueError('Перенаправлення; потрібна перевірка адреси: ' + url)
                if response.status_code in (401, 403, 429):
                    raise PermissionError(f'HTTP {response.status_code}; збір зупинено: {url}')
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                if any(marker in response.text[:20000].lower() for marker in
                       ('<title>just a moment', 'id="challenge-form"', 'cf-chl-')):
                    raise PermissionError('Сторінка перевірки безпеки; збір зупинено')
                response.encoding = 'utf-8'
                return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))


def xml_root(content):
    if content[:2] == b'\x1f\x8b':
        content = gzip.decompress(content)
    return ET.fromstring(content)


def in_dates(value, args):
    if not value:
        return True
    day = date.fromisoformat(value[:10])
    return not ((args.date_from and day < args.date_from) or (args.date_to and day > args.date_to))


def initialize(db, args):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS maps(url TEXT PRIMARY KEY,done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs(
            url TEXT PRIMARY KEY, category TEXT, published_hint TEXT, sort_key TEXT,
            record TEXT, error TEXT, skipped TEXT);
        CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(record,category);
    ''')
    settings = json.dumps({'categories': sorted(args.categories), 'date_from': str(args.date_from),
                           'date_to': str(args.date_to), 'seed': args.seed}, sort_keys=True)
    existing = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
    if existing and existing[0] != settings:
        raise ValueError('Дати, рубрики або seed відрізняються від першого запуску. '
                         'Повторіть початкові параметри або вкажіть іншу папку --output.')
    with db:
        db.execute("INSERT OR IGNORE INTO meta VALUES ('settings',?)", (settings,))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('snapshot_before',?)", (now(),))
    return db.execute("SELECT value FROM meta WHERE key='snapshot_before'").fetchone()[0]


def discover(client, db, args):
    if not db.execute("SELECT 1 FROM meta WHERE key='index_saved'").fetchone():
        root = xml_root(client.get(BASE + '/sitemap.xml').content)
        if root.tag.rsplit('}', 1)[-1] != 'sitemapindex':
            raise ValueError('Змінилася структура головної карти сайту')
        maps = []
        for node in root.findall('s:sitemap/s:loc', NS):
            url = canonical(node.text.strip())
            if urlsplit(url).path.startswith('/sitemap/'):
                maps.append(url)
        if not maps:
            raise ValueError('Не знайдено вкладених карт сайту')
        with db:
            db.executemany('INSERT OR IGNORE INTO maps(url) VALUES (?)', [(u,) for u in maps])
            db.execute("INSERT INTO meta VALUES ('index_saved','1')")
    query = 'SELECT url FROM maps WHERE done=0 ORDER BY rowid'
    if args.max_sitemaps:
        query += f' LIMIT {args.max_sitemaps}'
    maps = [r[0] for r in db.execute(query)]
    for index, url in enumerate(maps, 1):
        root = xml_root(client.get(url).content)
        if root.tag.rsplit('}', 1)[-1] != 'urlset':
            raise ValueError('Неочікувана вкладена карта: ' + url)
        entries = []
        for node in root.findall('s:url', NS):
            loc = node.findtext('s:loc', default='', namespaces=NS)
            try:
                link = canonical(loc)
            except ValueError:
                continue
            match = ARTICLE_PATH.fullmatch(urlsplit(link).path)
            if not match or match[1] not in args.categories:
                continue
            language = node.findtext('n:news/n:publication/n:language', default='', namespaces=NS)
            if language and language.lower() not in ('uk', 'ua', 'uk-ua'):
                continue
            published = node.findtext('n:news/n:publication_date', default='', namespaces=NS)
            if published and not in_dates(published, args):
                continue
            entries.append((link, match[1], published, digest(f'{args.seed}:{link}')))
        with db:
            db.executemany('INSERT OR IGNORE INTO jobs(url,category,published_hint,sort_key) VALUES (?,?,?,?)', entries)
            db.execute('UPDATE maps SET done=1 WHERE url=?', (url,))
        print(f'[Карта {index}/{len(maps)}] {url}: {len(entries)} адрес у вибраних рубриках', flush=True)
    count = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    pending = db.execute('SELECT count(*) FROM maps WHERE done=0').fetchone()[0]
    print(f'У черзі {count} адрес. Непрочитаних карт: {pending}.', flush=True)


def parse_article(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.news')
    if article is None:
        raise ValueError('Не знайдено article.news; перевірте розмітку')
    heading = article.select_one('h1.newsTitle')
    body = article.select_one('.newsText')
    if heading is None or body is None:
        raise ValueError('Не знайдено заголовок або текст статті')
    title = clean(heading.get_text(' ', strip=True))
    match = ARTICLE_PATH.fullmatch(urlsplit(url).path)
    if not match or match[1] not in RUBRICS:
        raise SkipArticle('excluded_rubric')
    html_language = soup.html.get('lang', '') if soup.html else ''
    if html_language and html_language.lower().split('-')[0] not in ('uk', 'ua'):
        raise SkipArticle('non_ukrainian_page')
    canonical_tag = soup.select_one('link[rel="canonical"]')
    if canonical_tag and canonical_tag.get('href'):
        candidate_url = canonical(canonical_tag['href'])
        candidate_match = ARTICLE_PATH.fullmatch(urlsplit(candidate_url).path)
        if not candidate_match or candidate_match[2] != match[2]:
            raise ValueError('Канонічна адреса вказує на інший матеріал')
    time_node = article.select_one('time[datetime]')
    if not time_node:
        raise ValueError('Не знайдено дату публікації')
    published = time_node['datetime']
    datetime.fromisoformat(published.replace('Z', '+00:00'))
    if len(title) < 20:
        raise SkipArticle('short_title')
    if re.search(r'(?i)^(?:фейк|маніпуляція|фактчек|спростування)\s*[:—–-]', title):
        raise SkipArticle('factcheck_title')
    tags = sorted({clean(a.get_text(' ', strip=True)) for a in article.select('.tags a.tag')})
    if any(t.casefold() in ('фейк', 'фейки', 'фактчек', 'фактчеки', 'фактчекінг', 'спростування') for t in tags):
        raise SkipArticle('factcheck_tag')
    badges = [clean(n.get_text(' ', strip=True)).casefold() for n in article.select('header .newsPrefix, header .newsType')]
    if any(t in ('блог', 'блоги', 'колонка', 'думка', 'реклама', 'новини партнерів',
                 'пресреліз', 'прес-реліз', 'аналітика', "інтерв'ю", 'інтерв’ю',
                 'фактчек', 'фактчеки') for t in badges):
        raise SkipArticle('non_news_badge')
    media = bool(body.select('iframe,video,embed'))
    for node in list(body.select('script,style,iframe,video,embed,form,figure,section.read,'
                                 '.read-also,.related,.mobileBanner,[data-name],ins')):
        node.decompose()
    links = []
    seen_links = set()
    for a in body.select('a[href]'):
        target = urljoin(url, a['href'])
        if urlsplit(target).scheme in ('http', 'https') and target not in seen_links:
            seen_links.add(target)
            links.append({'url': target, 'anchor': clean(a.get_text(' ', strip=True)), 'role': 'unreviewed'})
    lead_node = body.select_one('.newsHeading')
    lead = clean(lead_node.get_text(' ', strip=True)) if lead_node else ''
    if lead_node:
        lead_node.decompose()
    paragraphs = []
    if lead:
        paragraphs.append(lead)
    for node in body.select('p,h2,h3,h4,li,blockquote,tr'):
        if node.find_parent(['p', 'li', 'blockquote', 'tr']):
            continue
        text = clean(node.get_text(' ', strip=True))
        if not text or re.match(r'(?i)^(?:Читайте також\s*:|Фото\s*:|Фото\s+ілюстративне)', text):
            continue
        if not paragraphs or paragraphs[-1] != text:
            paragraphs.append(text)
    text = '\n\n'.join(paragraphs)
    if len(text) < 200:
        raise SkipArticle('short_text_or_video')
    return {
        'schema_version': '1.1', 'id': 'ukrinform_' + match[2], 'source': 'ukrinform',
        'record_type': 'news_article', 'url': url, 'published_at': published,
        'collected_at': now(), 'title_original': title, 'article_text': text,
        'claim': None, 'claim_candidate': title, 'original_text': text, 'original_url': url,
        'label': 'true', 'suggested_label': 'true', 'label_basis': 'source_based',
        'verdict_original': None, 'review_status': 'not_individually_verified',
        'categories': tags, 'archive_categories': [match[1]],
        'media_type': 'mixed' if media else 'text', 'links': links, 'evidence_urls': [],
        'text_hash': digest(clean(text).casefold()), 'duplicate_urls': [],
        'metadata': {'content_status': 'text', 'published_at_original': published,
                     'rubric': RUBRICS[match[1]], 'language_hint': 'uk',
                     'parser_version': '1.0', 'label_quality': 'weak',
                     'label_note': 'true присвоєно за джерелом, без індивідуального фактчекінгу.'}
    }


def language_detector():
    from lingua import Language, LanguageDetectorBuilder
    return LanguageDetectorBuilder.from_languages(
        Language.UKRAINIAN, Language.RUSSIAN, Language.ENGLISH,
        Language.BELARUSIAN, Language.POLISH).build()


def collect(client, db, args, snapshot):
    detector = language_detector()
    counts = Counter(dict(db.execute('SELECT category,count(*) FROM jobs WHERE record IS NOT NULL GROUP BY category')))
    known = {row[0] for row in db.execute('SELECT json_extract(record,\'$.metadata.dedup_key\') FROM jobs WHERE record IS NOT NULL')}
    query = 'SELECT url,category FROM jobs WHERE record IS NULL AND skipped IS NULL'
    if not args.retry_errors:
        query += ' AND error IS NULL'
    query += ' ORDER BY sort_key,url'
    # Після повного discovery порядок перемішаний по всьому доступному періоду.
    queue = db.execute(query).fetchall()
    attempts = 0
    snapshot_time = datetime.fromisoformat(snapshot)
    for url, category in queue:
        if args.per_category and counts[category] >= args.per_category:
            continue
        if args.max_articles and attempts >= args.max_articles:
            break
        attempts += 1
        try:
            response = client.get(url)
            record = parse_article(response.text, url)
            published = datetime.fromisoformat(record['published_at'].replace('Z', '+00:00'))
            if not in_dates(record['published_at'], args):
                raise SkipArticle('outside_date_range')
            if published.tzinfo is None:
                raise ValueError('Дата публікації не містить часового поясу')
            if published > snapshot_time:
                raise SkipArticle('after_snapshot')
            values = detector.compute_language_confidence_values(record['title_original'] + '\n' + record['article_text'][:6000])
            best = values[0]
            lang = best.language.name.lower()
            if lang != 'ukrainian':
                raise SkipArticle('language_' + lang)
            if best.value < 0.8:
                raise SkipArticle('language_uncertain')
            record['metadata'].update({'language_detected': 'uk', 'language_score': round(best.value, 6),
                                       'language_method': 'lingua_5_languages', 'requires_language_check': False})
            key = digest(clean(record['title_original']).casefold() + '\n' + clean(record['article_text']).casefold())
            record['metadata']['dedup_key'] = key
            with db:
                db.execute('UPDATE jobs SET record=?,error=NULL WHERE url=?', (json.dumps(record, ensure_ascii=False), url))
            counts[category] += 1
            duplicate = key in known
            known.add(key)
            print(f'[{attempts}] [{RUBRICS[category]}] {record["title_original"]}' + (' [дублікат]' if duplicate else ''), flush=True)
        except SkipArticle as exc:
            with db:
                db.execute('UPDATE jobs SET skipped=?,error=NULL WHERE url=?', (str(exc), url))
            print(f'[{attempts}] Пропущено: {exc}', flush=True)
        except PermissionError:
            raise
        except (requests.RequestException, ValueError, KeyError) as exc:
            with db:
                db.execute('UPDATE jobs SET error=? WHERE url=?', (str(exc), url))
            print(f'[{attempts}] Помилка: {url}: {exc}', flush=True)


def export(db, folder):
    unique = {}
    stored = 0
    for (raw,) in db.execute('SELECT record FROM jobs WHERE record IS NOT NULL ORDER BY rowid'):
        stored += 1
        record = json.loads(raw)
        key = record['metadata']['dedup_key']
        if key in unique:
            unique[key]['duplicate_urls'].append(record['url'])
        else:
            unique[key] = record
    errors = [{'url': u, 'error': e} for u, e in db.execute('SELECT url,error FROM jobs WHERE error IS NOT NULL')]
    dates = [r['published_at'][:10] for r in unique.values()]
    summary = {
        'stored_records': stored, 'unique_exported': len(unique),
        'duplicates_merged': stored - len(unique), 'labels': {'true': len(unique)},
        'label_basis': {'source_based': len(unique)},
        'by_category': dict(Counter(r['metadata']['rubric'] for r in unique.values())),
        'date_range_exported': [min(dates), max(dates)] if dates else [],
        'pending_posts': db.execute('SELECT count(*) FROM jobs WHERE record IS NULL AND skipped IS NULL AND error IS NULL').fetchone()[0],
        'pending_sitemaps': db.execute('SELECT count(*) FROM maps WHERE done=0').fetchone()[0],
        'skipped': dict(db.execute('SELECT skipped,count(*) FROM jobs WHERE skipped IS NOT NULL GROUP BY skipped')),
        'errors': len(errors), 'snapshot_before': dict(db.execute('SELECT key,value FROM meta')).get('snapshot_before'),
        'note': 'true — слабка мітка за джерелом, не результат індивідуальної перевірки. '
                'pending_posts може залишатися через квоти рубрик. Фільтр жанрів евристичний.'
    }
    path = folder / 'articles.jsonl'
    temp = path.with_suffix('.jsonl.tmp')
    with temp.open('w', encoding='utf-8') as f:
        for record in unique.values():
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    temp.replace(path)
    for name, content in [('summary.json', summary), ('errors.json', errors)]:
        target = folder / name
        temp = target.with_suffix('.json.tmp')
        temp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('Файл:', path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, default=Path('data/raw/ukrinform'))
    parser.add_argument('--max-articles', type=int, default=10, help='Нових спроб за запуск; 0 — без ліміту')
    parser.add_argument('--max-sitemaps', type=int, default=0, help='Нових карт за запуск; 0 — усі')
    parser.add_argument('--per-category', type=int, default=1500, help='До стількох збережених записів на рубрику; 0 — усі')
    parser.add_argument('--categories', nargs='+', choices=RUBRICS, default=DEFAULT_CATEGORIES)
    parser.add_argument('--date-from', type=date.fromisoformat)
    parser.add_argument('--date-to', type=date.fromisoformat)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--delay', type=float, default=2)
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--export-only', action='store_true')
    args = parser.parse_args()
    if min(args.max_articles, args.max_sitemaps, args.per_category) < 0:
        parser.error('Ліміти мають бути >= 0')
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error('--date-from має бути не пізніше --date-to')
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / 'state.sqlite3')
    initialized = False
    client = None
    try:
        # export-only не потребує повторення параметрів первинного запуску.
        if args.export_only:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchone():
                raise ValueError('Ще немає стану для експорту')
            initialized = True
            return
        snapshot = initialize(db, args)
        initialized = True
        client = Client(args.delay)
        discover(client, db, args)
        collect(client, db, args, snapshot)
    except KeyboardInterrupt:
        print('\nЗупинено. Завершені записи й карти збережено; повторіть команду для продовження.')
    except (requests.RequestException, PermissionError, ValueError, ET.ParseError, ImportError) as exc:
        print('Збір зупинено:', exc)
    finally:
        if initialized:
            export(db, args.output)
        if client:
            client.session.close()
        db.close()


if __name__ == '__main__':
    main()
