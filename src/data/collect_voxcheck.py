"""Збір архівів VoxCheck і медичної бази в спільний формат JSONL."""
import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = 'https://voxukraine.org'
API = BASE + '/wp-json/wp/v2'
ARCHIVES = {'voxcheck': 'voxcheck-uk', 'debunks': 'voks-informue'}
SHEET = 'https://docs.google.com/spreadsheets/d/1rMCP_E1FVbKn4kKvN8l7Bg-ZKv4qgYBledBKZUUP0hg/export?format=csv'
AGENT = 'BachelorThesisResearch/1.0'
PREFIX = re.compile(
    r'^\s*(ОБЕРЕЖНО\s*,\s*ШАХРАЇ|ТЕХНІЧНА ПОМИЛКА|БЕЗ ВЕРДИКТУ|'
    r'ПЕРЕБІЛЬШЕННЯ|КОНСПІРОЛОГІЯ|МАНІПУЛЯЦІЯ|БЕЗПІДСТАВНО|'
    r'ФОТОФЕЙК|ВІДЕОФЕЙК|НЕПРАВДА|ПРАВДА|САТИРА|ФЕЙК)\s*[:：]\s*', re.I)
LABELS = {'фейк': 'false', 'неправда': 'false', 'фотофейк': 'false',
          'відеофейк': 'false', 'маніпуляція': 'manipulation', 'правда': 'true'}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), '', ''))


def html_text(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    for node in soup.select('script, style, iframe'):
        node.decompose()
    lines = [clean(n.get_text(' ', strip=True)) for n in soup.select('p, h2, h3, h4, li, blockquote, tr')
             if not n.find_parent(['li', 'blockquote', 'tr'])]
    lines = [x for x in lines if x and not x.casefold().startswith(('скриншот', 'скріншот'))
             and not re.fullmatch(r'Перевірка фейків у рамках партнерства з (?:Meta|Facebook)[.!]?', x, re.I)]
    return '\n\n'.join(lines) if lines else clean(soup.get_text(' ', strip=True))


def html_links(html, base, section):
    soup = BeautifulSoup(html or '', 'html.parser')
    result = []
    seen = set()
    for a in soup.select('a[href]'):
        url = urljoin(base, a['href'])
        if urlsplit(url).scheme not in ('http', 'https') or url in seen:
            continue
        if re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', url, re.I):
            continue
        seen.add(url)
        parent = a.find_parent(['p', 'li'])
        result.append({'url': url, 'anchor': clean(a.get_text(' ', strip=True)),
                       'context': clean(parent.get_text(' ', strip=True)) if parent else '',
                       'role': 'unreviewed', 'section': section})
    return result


def base_record(identifier, url, title, text, record_type):
    return {
        'schema_version': '1.0', 'id': identifier, 'source': 'voxcheck', 'url': url,
        'published_at': None, 'collected_at': now(), 'categories': [],
        'archive_categories': [], 'title_original': title, 'article_text': text,
        'claim': None, 'claim_candidate': None, 'original_text': None,
        'original_url': None, 'verdict_original': None, 'label': None,
        'media_type': 'unspecified', 'links': [], 'evidence_urls': [],
        'review_status': 'pending', 'text_hash': digest(clean(text).casefold()),
        'duplicate_urls': [], 'record_type': record_type,
        'original_occurrences': [], 'metadata': {},
    }


def parse_post(post):
    if post.get('content', {}).get('protected'):
        raise ValueError('Закритий матеріал: автоматичне опрацювання пропущено.')
    url = canonical(post['link'])
    p = urlsplit(url)
    if p.hostname not in ('voxukraine.org', 'www.voxukraine.org') or p.path.startswith(('/en/', '/ru/')):
        raise ValueError('Посилання не належить українській версії VoxUkraine.')
    title = clean(BeautifulSoup(post['title']['rendered'], 'html.parser').get_text(' ', strip=True))
    html = post['content']['rendered']
    text = html_text(html)
    short = len(text) < 150
    record = base_record('voxcheck_' + str(post['id']), url, title, text, 'factcheck_article')
    date = post.get('date_gmt')
    record['published_at'] = date + 'Z' if date and date != '0000-00-00T00:00:00' else None
    record['metadata'] = {'wp_post_id': post['id'], 'wp_category_ids': post.get('categories', []),
                          'wp_tag_ids': post.get('tags', []), 'modified_at_gmt': post.get('modified_gmt')}
    for group in post.get('_embedded', {}).get('wp:term', []):
        if isinstance(group, list):
            record['categories'].extend(clean(t['name']) for t in group
                                        if t.get('taxonomy') == 'category' and t.get('name'))
    record['categories'] = sorted(set(record['categories']))
    record['links'] = html_links(html, url, 'article')
    record['metadata']['parser_version'] = '2.0'
    record['metadata']['content_status'] = ('video_only' if short and BeautifulSoup(html, 'html.parser').select_one('iframe')
                                            else 'short_text' if short else 'text')
    match = PREFIX.match(title)
    if match:
        verdict = clean(match.group(1))
        record['verdict_original'] = verdict
        record['label'] = LABELS.get(verdict.casefold())
        record['claim_candidate'] = title[match.end():].strip()
        record['media_type'] = {'фотофейк': 'image', 'відеофейк': 'video'}.get(verdict.casefold(), 'unspecified')
    if short:
        record['label'] = None
        record['review_status'] = 'excluded_short_content'
    return record


def parse_medical(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
    required = {'id', 'subtheme', 'discription', 'disproof', 'verdict', 'link', 'date'}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError('Медична таблиця має інші колонки або замість CSV отримано іншу сторінку.')
    rows = list(reader)
    groups = {}
    for row in rows:
        if not any((v or '').strip() for v in row.values()):
            continue
        title = clean(row['subtheme'])
        description = html_text(row['discription'])
        explanation = html_text(row['disproof'])
        verdict = clean(row['verdict'])
        if not title or not description or not explanation or not row['id']:
            raise ValueError(f'Неповний рядок медичної таблиці: {row.get("id")}')
        # Об'єднуємо лише однакове формулювання, опис, пояснення й вердикт.
        key = digest(json.dumps([title, description, explanation, verdict], ensure_ascii=False))
        if key not in groups:
            record = base_record('voxcheck_medical_' + key[:20], SHEET, title,
                                 description + '\n\n' + explanation, 'medical_database_entry')
            record['categories'] = ['Медична база VoxCheck']
            record['archive_categories'] = ['medical']
            record['claim_candidate'] = title
            record['verdict_original'] = verdict
            # Тип інформаційного порушення не перетворюємо на фактологічну мітку.
            record['label'] = None
            record['links'] = html_links(row['discription'], BASE, 'description') + html_links(row['disproof'], BASE, 'explanation')
            record['metadata'] = {'dataset_url': SHEET, 'database_row_ids': [],
                                  'themes': [], 'countries': [], 'tags_original': [],
                                  'vox_article_ids': [], 'description': description, 'explanation': explanation}
            groups[key] = record
        record = groups[key]
        record['metadata']['database_row_ids'].append(row['id'])
        for column, field in [('theme', 'themes'), ('country', 'countries'),
                              ('tags', 'tags_original'), ('vox_article_id', 'vox_article_ids')]:
            value = clean(row.get(column))
            if value and value not in record['metadata'][field]:
                record['metadata'][field].append(value)
        # Дата таблиці належить оригінальній публікації, не даті фактчеку.
        record['original_occurrences'].append({
            'row_id': row['id'], 'url': row['link'].strip(), 'source': clean(row.get('source')),
            'date_original': clean(row['date']), 'video_id': clean(row.get('video_id')),
            'infographic_id': clean(row.get('infographic_id'))})
    return list(groups.values()), len([r for r in rows if r.get('id')])


class Client:
    def __init__(self, delay):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'uk'})
        self.delay = delay
        self.last = 0.0
        self.robots = None

    def get(self, url, params=None):
        prepared = requests.Request('GET', url, params=params).prepare().url
        if self.robots and urlsplit(url).hostname == 'voxukraine.org' and not self.robots.can_fetch(AGENT, prepared):
            raise RuntimeError(f'robots.txt забороняє завантаження: {url}')
        for attempt in range(4):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=(15, 60))
                if response.status_code in (401, 403):
                    raise RuntimeError(f'Доступ відхилено ({response.status_code}). Збір зупинено без обходу захисту.')
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    retry = response.headers.get('Retry-After', '')
                    time.sleep(min(int(retry) if retry.isdigit() else 5 * 2 ** attempt, 60))
                    continue
                response.raise_for_status()
                response.encoding = 'utf-8'
                return response
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def load_robots(self):
        response = self.get(BASE + '/robots.txt')
        self.robots = RobotFileParser()
        self.robots.parse(response.text.replace('\\r\\n', '\n').splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(AGENT) or 0)


def metadata(db, key, default=None):
    row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_metadata(db, key, value):
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
    db.commit()


def collect_archive(db, client, directory, category, args):
    category_key = 'category:' + category
    info = metadata(db, category_key)
    if info is None:
        response = client.get(API + '/categories', {'slug': ARCHIVES[category], 'lang': 'uk'})
        items = response.json()
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError(f'Не вдалося однозначно знайти рубрику {category}')
        info = {'id': items[0]['id'], 'count': items[0]['count'], 'name': items[0]['name']}
        set_metadata(db, category_key, info)
    page = 1
    last = None
    while last is None or page <= last:
        if args.max_pages and page > args.max_pages:
            break
        page_key = f'{category}:{page}'
        saved = db.execute('SELECT total_pages FROM pages WHERE key=?', (page_key,)).fetchone()
        if saved:
            last = saved[0]
            print(f'[{category}] Сторінка API {page}/{last}: уже збережена', flush=True)
            page += 1
            continue
        response = client.get(API + '/posts', {
            'categories': info['id'], 'lang': 'uk', 'per_page': 50, 'page': page,
            'orderby': 'date', 'order': 'desc', 'before': metadata(db, 'snapshot_before'),
            '_embed': 'wp:term',
        })
        posts = response.json()
        if not isinstance(posts, list):
            raise ValueError('API повернув не список публікацій.')
        if not posts:
            break
        last = int(response.headers.get('X-WP-TotalPages', '0'))
        if last < 1:
            raise ValueError('API не надав кількість сторінок; збір зупинено.')
        for post in posts:
            key = 'post:' + str(post['id'])
            db.execute('INSERT OR IGNORE INTO jobs(key,payload) VALUES (?,?)',
                       (key, json.dumps(post, ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (key, category))
        db.execute('INSERT INTO pages VALUES (?,?)', (page_key, last))
        db.commit()
        print(f'[{category}] Сторінка API {page}/{last}: {len(posts)} публікацій', flush=True)
        page += 1


def collect_medical(db, client, directory):
    if metadata(db, 'medical_complete'):
        print('[medical] Таблиця вже збережена', flush=True)
        return
    response = client.get(SHEET)
    if 'csv' not in response.headers.get('Content-Type', '').lower():
        raise ValueError('Замість медичної CSV-таблиці отримано інший формат.')
    records, rows = parse_medical(response.text)
    for record in records:
        key = record['id']
        db.execute('INSERT OR REPLACE INTO jobs(key,record) VALUES (?,?)',
                   (key, json.dumps(record, ensure_ascii=False)))
        db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (key, 'medical'))
    set_metadata(db, 'medical_complete', {'rows': rows, 'grouped_records': len(records)})
    db.commit()
    print(f'[medical] {rows} рядків → {len(records)} перевірок зі збереженням усіх джерел', flush=True)


def export(db, directory):
    records = {}
    total = 0
    for key, raw in db.execute('SELECT key,record FROM jobs WHERE record IS NOT NULL UNION ALL SELECT key,record FROM claims ORDER BY key'):
        total += 1
        record = json.loads(raw)
        record['archive_categories'] = sorted({r[0] for r in db.execute(
            'SELECT category FROM memberships WHERE key=?', (key,))})
        basis = {'verified_claim': 'explicit_claim_verdict',
                 'factcheck_article': 'explicit_title_verdict'}.get(record['record_type'])
        record.setdefault('label_basis', basis if record['label'] else None)
        record['metadata']['search_only'] = record['archive_categories'] == ['claim_search']
        match_key = ((record['record_type'], record['id']) if record['record_type'] in ('medical_database_entry', 'verified_claim')
                     else (record['record_type'], record['text_hash'], record['verdict_original'], record['label']))
        if match_key in records:
            previous = records[match_key]
            if record['url'] != previous['url']:
                previous['duplicate_urls'].append(record['url'])
            for field in ('categories', 'archive_categories'):
                previous[field] = sorted(set(previous[field] + record[field]))
        else:
            records[match_key] = record
    files = {'articles.jsonl': lambda r: True}
    for name, include in files.items():
        path = directory / name
        temp = path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as output:
            for record in records.values():
                if include(record):
                    output.write(json.dumps(record, ensure_ascii=False) + '\n')
        temp.replace(path)
    stats = {
        'stored_records': total, 'unique_exported': len(records),
        'labels': dict(Counter(r['label'] or 'unclassified' for r in records.values())),
        'record_types': dict(Counter(r['record_type'] for r in records.values())),
        'medical': metadata(db, 'medical_complete'),
        'claim_labels': dict(Counter(r['label'] or 'unclassified' for r in records.values() if r['record_type'] == 'verified_claim')),
        'content_status': dict(Counter(r.get('metadata', {}).get('content_status', 'other') for r in records.values() if r['record_type'] == 'factcheck_article')),
        'pending_posts': db.execute('SELECT COUNT(*) FROM jobs WHERE record IS NULL').fetchone()[0],
        'errors': db.execute('SELECT COUNT(*) FROM jobs WHERE error IS NOT NULL').fetchone()[0],
        'snapshot_before': metadata(db, 'snapshot_before'),
        'note': 'Сирий корпус: articles містить і статті, і виділені твердження. Для claim-level аналізу дивіться claim_labels. Усі записи потребують перевірки.',
    }
    (directory / 'summary.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    errors = [{'id': r[0], 'error': r[1]} for r in db.execute('SELECT key,error FROM jobs WHERE error IS NOT NULL')]
    (directory / 'errors.json').write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    print(f'Результати: {directory / "articles.jsonl"}', flush=True)



def extract_claims(post, parent):
    """Консервативно: окремий вердикт і сусідня цитата, не згадка слова у прозі."""
    soup = BeautifulSoup(post['content']['rendered'], 'html.parser')
    for node in soup.select('script, style, iframe'):
        node.decompose()
    blocks = [n for n in soup.select('p, h1, h2, h3, h4, li, blockquote, tr')
              if not n.find_parent(['p', 'li', 'blockquote', 'tr']) and clean(n.get_text(' ', strip=True))]
    verdicts = set(LABELS) | {'перебільшення', 'технічна помилка', 'без вердикту',
                             'безпідставно', 'сатира', 'конспірологія', 'обережно, шахраї'}

    def marker(node):
        value = clean(node.get_text(' ', strip=True)).casefold().rstrip(' .:!')
        value = re.sub(r'^вердикт\s*:\s*', '', value)
        return value if value in verdicts else None

    def quoted(node):
        value = clean(node.get_text(' ', strip=True))
        if len(value) < 30 or len(value) > 5000 or marker(node):
            return False
        return node.name == 'blockquote' or bool(re.match(r'^[«“„"]', value) and re.search(r'[»”"]\s*[.!?…]?$', value))

    found = {}
    for index, block in enumerate(blocks):
        verdict = marker(block)
        if not verdict:
            continue
        following = index + 1 if index + 1 < len(blocks) and quoted(blocks[index + 1]) else None
        preceding = index - 1 if index > 0 and quoted(blocks[index - 1]) else None
        # Два можливі сусіди без спільного контейнера означають неоднозначність.
        if following is not None and preceding is not None:
            if blocks[following].parent is block.parent and block.parent.name != '[document]':
                claim_index = following
            else:
                continue
        else:
            claim_index = following if following is not None else preceding
        if claim_index is None:
            continue
        quote = clean(blocks[claim_index].get_text(' ', strip=True))
        explanation = []
        j = max(index, claim_index) + 1
        while j < len(blocks):
            node = blocks[j]
            if marker(node) or node.name in ('h1', 'h2', 'h3', 'h4'):
                break
            # Цитата перед наступним вердиктом належить наступному блоку.
            if quoted(node) and j + 1 < len(blocks) and marker(blocks[j + 1]):
                break
            explanation.append(str(node))
            j += 1
        explanation_html = '\n'.join(explanation)
        justification = html_text(explanation_html)
        if len(justification) < 80:
            continue
        identifier = parent['id'] + '_claim_' + digest(quote + '\n' + verdict)[:16]
        record = base_record(identifier, parent['url'], parent['title_original'], justification, 'verified_claim')
        record.update({'published_at': parent['published_at'], 'categories': parent['categories'],
                       'claim': quote, 'claim_candidate': quote,
                       'verdict_original': clean(block.get_text(' ', strip=True)),
                       'label': LABELS.get(verdict),
                       'links': html_links(explanation_html, parent['url'], 'claim_explanation')})
        record['metadata'] = {'parent_id': parent['id'], 'wp_post_id': post['id'],
                              'parser_version': '2.0', 'extraction_method': 'explicit_verdict_and_adjacent_quote',
                              'verdict_block_index': index, 'claim_block_index': claim_index,
                              'justification': justification, 'requires_context_review': True}
        record['text_hash'] = digest(clean(quote).casefold())
        found[identifier] = record
    return list(found.values())


def store_post(db, key, post, record):
    old = db.execute('SELECT record FROM jobs WHERE key=?', (key,)).fetchone()
    if old and old[0]:
        record['collected_at'] = json.loads(old[0]).get('collected_at', record['collected_at'])
    record['metadata']['reprocessed_at'] = now()
    children = extract_claims(post, record)
    # У разі збою старий запис і його дочірні твердження залишаться узгодженими.
    with db:
        previous = [r[0] for r in db.execute('SELECT key FROM claims WHERE parent=?', (key,))]
        for child_key in previous:
            db.execute('DELETE FROM memberships WHERE key=?', (child_key,))
        db.execute('DELETE FROM claims WHERE parent=?', (key,))
        db.execute('UPDATE jobs SET record=?,error=NULL WHERE key=?', (json.dumps(record, ensure_ascii=False), key))
        categories = [r[0] for r in db.execute('SELECT category FROM memberships WHERE key=?', (key,))]
        for child in children:
            child_key = 'claim:' + child['id']
            db.execute('INSERT INTO claims VALUES (?,?,?)', (child_key, key, json.dumps(child, ensure_ascii=False)))
            for category in categories:
                db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (child_key, category))
    return len(children)


def reprocess(db):
    count = 0
    claims_count = 0
    cursor = db.execute('SELECT key,payload FROM jobs WHERE payload IS NOT NULL ORDER BY key')
    while True:
        batch = cursor.fetchmany(100)
        if not batch:
            break
        for key, payload in batch:
            try:
                post = json.loads(payload)
                record = parse_post(post)
                claims_count += store_post(db, key, post, record)
            except (ValueError, KeyError, TypeError) as exc:
                with db:
                    db.execute('UPDATE jobs SET error=? WHERE key=?', (str(exc), key))
                print(f'ПОМИЛКА {key}: {exc}', flush=True)
            count += 1
        print(f'Повторно опрацьовано: {count}; виділено тверджень: {claims_count}', flush=True)
    set_metadata(db, 'parser_version', '2.0')


def discover_claims(db, client, args):
    # Пошук потрібен, бо перевірки політичних цитат бувають поза двома архівами.
    for query in ('правда', 'фактчек', 'перевірка цитат'):
        page = 1
        last = None
        while last is None or page <= last:
            if args.max_pages and page > args.max_pages:
                break
            page_key = 'claim_search:' + query + ':' + str(page)
            saved = db.execute('SELECT total_pages FROM pages WHERE key=?', (page_key,)).fetchone()
            if saved:
                last = saved[0]
                page += 1
                continue
            response = client.get(API + '/posts', {'search': query, 'lang': 'uk', 'per_page': 50,
                'page': page, 'before': metadata(db, 'snapshot_before'), '_embed': 'wp:term'})
            posts = response.json()
            if not isinstance(posts, list):
                raise ValueError('Пошук повернув не список публікацій.')
            if not posts:
                break
            last = int(response.headers.get('X-WP-TotalPages', '0'))
            if last < 1:
                raise ValueError('Пошук не повернув кількість сторінок.')
            with db:
                for post in posts:
                    key = 'post:' + str(post['id'])
                    db.execute('INSERT OR IGNORE INTO jobs(key,payload) VALUES (?,?)', (key, json.dumps(post, ensure_ascii=False)))
                    db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (key, 'claim_search'))
                db.execute('INSERT INTO pages VALUES (?,?)', (page_key, last))
            print(f'[claim_search: {query}] {page}/{last}: {len(posts)} публікацій', flush=True)
            page += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', nargs='+', choices=[*ARCHIVES, 'medical'], default=[*ARCHIVES, 'medical'])
    parser.add_argument('--max-pages', type=int, default=1, help='Сторінок API на рубрику, до 50 записів; 0 — всі')
    parser.add_argument('--max-articles', type=int, default=10, help='Статей для опрацювання за запуск; 0 — всі. Не обмежує імпорт медичної таблиці.')
    parser.add_argument('--delay', type=float, default=2.0)
    parser.add_argument('--output', type=Path, default=Path('data/raw/voxcheck'))
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--reprocess', action='store_true', help='Повторно опрацювати всі збережені статті без інтернету')
    parser.add_argument('--discover-claims', action='store_true', help='Додатково знайти статті з перевірками цитат у всіх українських рубриках')
    args = parser.parse_args()
    if args.max_pages < 0 or args.max_articles < 0 or args.delay < 1:
        parser.error('Ліміти мають бути >= 0, затримка >= 1 секунди.')
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / 'state.sqlite3')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,payload TEXT,record TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS memberships(key TEXT,category TEXT,PRIMARY KEY(key,category));
        CREATE TABLE IF NOT EXISTS pages(key TEXT PRIMARY KEY,total_pages INTEGER);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS claims(key TEXT PRIMARY KEY,parent TEXT,record TEXT);
    ''')
    if args.export_only:
        export(db, args.output)
        db.close()
        return
    if args.reprocess:
        try:
            reprocess(db)
        except KeyboardInterrupt:
            print('Перервано. Повторний запуск безпечний.', flush=True)
        finally:
            export(db, args.output)
            db.close()
        return
    if metadata(db, 'snapshot_before') is None:
        set_metadata(db, 'snapshot_before', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    client = Client(args.delay)
    try:
        if args.discover_claims or any(s in ARCHIVES for s in args.sources):
            client.load_robots()
        # Медичний імпорт виконується окремо й повністю, навіть у тестовому запуску.
        if 'medical' in args.sources:
            collect_medical(db, client, args.output)
        for category in args.sources:
            if category in ARCHIVES:
                collect_archive(db, client, args.output, category, args)
        if args.discover_claims:
            discover_claims(db, client, args)
        selected = set(args.sources) | ({'claim_search'} if args.discover_claims else set())
        count = 0
        pending = list(db.execute('SELECT key,payload FROM jobs WHERE record IS NULL AND payload IS NOT NULL ORDER BY key'))
        for key, payload in pending:
            memberships = {r[0] for r in db.execute('SELECT category FROM memberships WHERE key=?', (key,))}
            if not selected.intersection(memberships):
                continue
            if args.max_articles and count >= args.max_articles:
                break
            try:
                record = parse_post(json.loads(payload))
                store_post(db, key, json.loads(payload), record)
                count += 1
                print(f'[{count}] {record["label"] or "без класу"}: {record["title_original"]}', flush=True)
            except (KeyError, TypeError, ValueError) as exc:
                db.execute('UPDATE jobs SET error=? WHERE key=?', (str(exc), key))
                print(f'ПОМИЛКА {key}: {exc}', flush=True)
            db.commit()
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        print(f'Збір зупинено: {exc}', flush=True)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print('\nЗупинено. Повторіть команду для продовження.', flush=True)
    finally:
        export(db, args.output)
        db.close()


if __name__ == '__main__':
    main()
