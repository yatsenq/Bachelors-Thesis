"""Збір українських фактчеків StopFake. Python 3.10+."""
import argparse
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = 'https://www.stopfake.org'
ARCHIVES = {
    'news': '/uk/category/novyny-ua/',
    'facebook': '/uk/category/factcheck_facebook_ua/',
    'covid': '/uk/category/covid19_ua/',
}
VERDICT = re.compile(r'^\s*(фотофейк|відеофейк|фейк|маніпуляція)\s*[:：]\s*', re.I)
AGENT = 'BachelorThesisResearch/1.0'


def clean(text):
    return re.sub(r'\s+', ' ', text).strip()


def canonical(url):
    p = urlsplit(urljoin(BASE, url))
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip('/') + '/', '', ''))


def article_url(url):
    p = urlsplit(url)
    return (p.hostname == 'www.stopfake.org' and p.path.startswith('/uk/')
            and len([x for x in p.path.split('/') if x]) == 2
            and not re.search(r'\.(jpg|png|pdf|webp)$', p.path, re.I))


def parse_archive(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    links = sorted({canonical(a['href']) for a in soup.select('.entry-title a[href]')
                    if article_url(urljoin(url, a['href']))})
    pages = [int(m.group(1)) for a in soup.select('.page-nav a[href]')
             if (m := re.search(r'/page/(\d+)/', a['href']))]
    if not links:
        raise ValueError('Не знайдено статей в архіві. Перевірте HTML або селектори.')
    return links, max(pages, default=1)


def parse_article(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h1.entry-title') or soup.select_one('h1')
    body = soup.select_one('.td-post-content') or soup.select_one('.tdb_single_content .tdb-block-inner')
    if not title_node or not body:
        raise ValueError('Не знайдено заголовок або основний текст статті.')
    for node in body.select('script, style, iframe, .sharedaddy, .td-post-sharing, .adsbygoogle'):
        node.decompose()
    title = clean(title_node.get_text(' ', strip=True))
    paragraphs = [clean(n.get_text(' ', strip=True)) for n in body.select('p, h2, h3, li, blockquote')
                  if not n.find_parent(['blockquote', 'li'])]
    paragraphs = [p for p in paragraphs if p and not p.casefold().startswith(('скриншот', 'скріншот'))]
    text = '\n\n'.join(paragraphs) or body.get_text('\n', strip=True)
    if len(text) < 150:
        raise ValueError('Замало тексту: статтю потрібно перевірити, запис не збережено як успішний.')
    links = []
    seen = set()
    for a in body.select('a[href]'):
        href = urljoin(url, a['href'])
        if urlsplit(href).scheme not in ('http', 'https') or href in seen:
            continue
        if re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', href, re.I):
            continue
        seen.add(href)
        parent = a.find_parent('p')
        links.append({'url': href, 'anchor': clean(a.get_text(' ', strip=True)),
                      'context': clean(parent.get_text(' ', strip=True)) if parent else '',
                      'role': 'unreviewed'})
    match = VERDICT.match(title)
    verdict = match.group(1) if match else None
    label = ('manipulation' if verdict.casefold() == 'маніпуляція' else 'false') if verdict else None
    date_node = soup.select_one('time[datetime]')
    categories = sorted({clean(a.get_text(' ', strip=True)) for a in soup.select('.td-category a')})
    return {
        'schema_version': '1.0',
        'id': 'stopfake_' + hashlib.sha256(url.encode()).hexdigest()[:20],
        'source': 'stopfake', 'url': url,
        'published_at': date_node.get('datetime') if date_node else None,
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'categories': categories, 'title_original': title, 'article_text': text,
        'claim': None, 'claim_candidate': title[match.end():].strip() if match else None,
        'original_text': None, 'original_url': None,
        'verdict_original': verdict, 'label': label,
        'media_type': ('image' if verdict and verdict.casefold() == 'фотофейк' else
                       'video' if verdict and verdict.casefold() == 'відеофейк' else 'unspecified'),
        'links': links, 'evidence_urls': [], 'review_status': 'pending',
        'text_hash': hashlib.sha256(clean(text).casefold().encode()).hexdigest(),
    }


class Client:
    def __init__(self, delay):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'uk'})
        self.delay = delay
        self.last_request = 0.0
        self.robots = None

    def get(self, url):
        if self.robots and not self.robots.can_fetch(AGENT, url):
            raise RuntimeError(f'robots.txt забороняє завантаження: {url}')
        for attempt in range(4):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(15, 60))
                if response.status_code in (429, 500, 502, 503, 504):
                    retry = response.headers.get('Retry-After', '')
                    wait = int(retry) if retry.isdigit() else 5 * 2 ** attempt
                    time.sleep(min(wait, 60))
                    response.raise_for_status()
                response.raise_for_status()
                if 'html' not in response.headers.get('Content-Type', '') and not url.endswith('robots.txt'):
                    raise ValueError(f'Очікував HTML, отримано {response.headers.get("Content-Type")}')
                response.encoding = 'utf-8'
                return response.text
            except requests.RequestException as exc:
                if exc.response is not None and exc.response.status_code in (401, 403):
                    raise RuntimeError('Сайт відхилив запит (401/403). Збір зупинено; захист не обходиться.') from exc
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def load_robots(self):
        response = self.session.get(BASE + '/robots.txt', timeout=30)
        response.raise_for_status()
        self.robots = RobotFileParser()
        self.robots.parse(response.text.splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(AGENT) or 0)


def export(db, directory):
    unique = {}
    total = 0
    for url, raw in db.execute('SELECT url, record FROM articles WHERE record IS NOT NULL ORDER BY url'):
        total += 1
        record = json.loads(raw)
        record['archive_categories'] = [x[0] for x in db.execute(
            'SELECT category FROM memberships WHERE url=? ORDER BY category', (url,))]
        record.setdefault('record_type', 'factcheck_article')
        record.setdefault('label_basis', 'explicit_title_verdict' if record['label'] else None)
        record.setdefault('metadata', {})
        key = (record['text_hash'], record['label'])
        if key in unique:
            first = unique[key]
            first['duplicate_urls'].append(url)
            for field in ('categories', 'archive_categories'):
                first[field] = sorted(set(first[field] + record[field]))
        else:
            record['duplicate_urls'] = []
            unique[key] = record
    path = directory / 'articles.jsonl'
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as output:
        for record in unique.values():
            output.write(json.dumps(record, ensure_ascii=False) + '\n')
    temp.replace(path)
    labels = {}
    for record in unique.values():
        label = record['label'] or 'unclassified'
        labels[label] = labels.get(label, 0) + 1
    errors = db.execute('SELECT COUNT(*) FROM articles WHERE record IS NULL AND error IS NOT NULL').fetchone()[0]
    stats = {'downloaded': total, 'unique_exported': len(unique), 'labels': labels,
             'errors': errors, 'note': 'Це сирий корпус перевірок, не готова навчальна вибірка.'}
    (directory / 'summary.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    print(f'Файл: {path}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--categories', nargs='+', choices=ARCHIVES, default=list(ARCHIVES))
    parser.add_argument('--max-pages', type=int, default=1, help='Сторінок кожної рубрики; 0 — всі')
    parser.add_argument('--max-articles', type=int, default=10, help='Нових успішних статей за запуск; 0 — всі')
    parser.add_argument('--delay', type=float, default=2.0)
    parser.add_argument('--output', type=Path, default=Path('data/raw/stopfake'))
    parser.add_argument('--export-only', action='store_true')
    args = parser.parse_args()
    if args.max_pages < 0 or args.max_articles < 0 or args.delay < 1:
        parser.error('Ліміти мають бути >= 0, затримка >= 1 секунди.')
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / 'state.sqlite3')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS articles(url TEXT PRIMARY KEY, record TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS memberships(url TEXT, category TEXT, PRIMARY KEY(url,category));
        CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, completed_at TEXT);
    ''')
    if args.export_only:
        export(db, args.output)
        db.close()
        return
    client = Client(args.delay)
    successful = 0
    try:
        client.load_robots()
        # Спочатку збираємо адреси в усіх вибраних рубриках, потім статті.
        for category in args.categories:
            root = BASE + ARCHIVES[category]
            page = 1
            last = 1
            while page <= last:
                url = root if page == 1 else root + f'page/{page}/'
                cached = db.execute('SELECT 1 FROM pages WHERE url=?', (url,)).fetchone()
                # Першу сторінку перечитуємо: кількість сторінок і нові статті змінюються.
                if cached and page > 1:
                    page += 1
                    continue
                links, discovered_last = parse_archive(client.get(url), url)
                if page == 1:
                    last = min(discovered_last, args.max_pages) if args.max_pages else discovered_last
                for link in links:
                    db.execute('INSERT OR IGNORE INTO articles(url) VALUES (?)', (link,))
                    db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (link, category))
                db.execute('INSERT OR REPLACE INTO pages VALUES (?,?)',
                           (url, datetime.now(timezone.utc).isoformat()))
                db.commit()
                print(f'[{category}] Сторінка {page}/{last}: {len(links)} адрес', flush=True)
                page += 1
        pending = [row[0] for row in db.execute('SELECT url FROM articles WHERE record IS NULL ORDER BY url')]
        print(f'Статей до завантаження: {len(pending)}', flush=True)
        for url in pending:
            if args.max_articles and successful >= args.max_articles:
                break
            try:
                html = client.get(url)
                record = parse_article(html, url)
                db.execute('UPDATE articles SET record=?, error=NULL WHERE url=?',
                           (json.dumps(record, ensure_ascii=False), url))
                successful += 1
                print(f'[{successful}] {record["title_original"]}', flush=True)
            except (requests.RequestException, ValueError) as exc:
                db.execute('UPDATE articles SET error=? WHERE url=?', (str(exc), url))
                print(f'ПОМИЛКА: {url}: {exc}', flush=True)
            db.commit()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f'Збір зупинено: {exc}', flush=True)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print('\nЗупинено. Збережені результати залишаються.', flush=True)
    finally:
        export(db, args.output)
        db.close()


if __name__ == '__main__':
    main()
