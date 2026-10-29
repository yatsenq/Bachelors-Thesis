"""Спільний збирач відкритих WordPress REST API. HTML і медіа не зберігає."""
import argparse
import hashlib
import html
import json
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

AGENT = 'BachelorThesisResearch/1.0'
LABELS = {'фейк': 'false', 'фотофейк': 'false', 'відеофейк': 'false',
          'неправда': 'false', 'брехня': 'false', 'правда': 'true',
          'маніпуляція': 'manipulation'}
VERDICT_WORD = r'(?:фотофейк|відеофейк|маніпуляція|неправда|правда|брехня|фейк)'
TITLE_PREFIX = re.compile(r'^\s*(' + VERDICT_WORD + r')\s*(?:[:.：—–-]\s*|\s+(?:[^:.\n]{1,100}?\s+)?про\s+(?:те\s*,?\s*що\s+)?)', re.I)
TITLE_SUFFIX = re.compile(r'^(.+?)\s+[—–-]\s*(' + VERDICT_WORD + r')\s*[.!]?\s*$', re.I)
THESIS = re.compile(r'^\s*(?:теза|твердження|цитата)\s*(?:№?\s*\d+)?\s*[:.：]\s*(.*)$', re.I)
BLOCKS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'tr']


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(unicodedata.normalize('NFC', str(value or '')))).strip()


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def text_of(markup):
    return clean(BeautifulSoup(markup or '', 'html.parser').get_text(' ', strip=True))


def canonical(url, base):
    p = urlsplit(urljoin(base, url))
    host = urlsplit(base).hostname
    if p.scheme not in ('http', 'https') or p.hostname not in (host, 'www.' + host):
        raise ValueError('Адреса поза вибраним сайтом: ' + url)
    return urlunsplit(('https', host, p.path or '/', '', ''))


class Client:
    def __init__(self, base, delay):
        self.base = base
        self.delay = max(1.0, delay)
        self.last = 0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': AGENT, 'Accept-Language': 'uk'})
        self.robots = None
        response = self.get(base + '/robots.txt')
        if not re.search(r'(?im)^user-agent\s*:', response.text):
            raise ValueError('Неочікуваний robots.txt; збір зупинено')
        self.robots = RobotFileParser()
        self.robots.parse(response.text.splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(AGENT) or 0)
        rate = self.robots.request_rate(AGENT)
        if rate and rate.requests:
            self.delay = max(self.delay, rate.seconds / rate.requests)

    def get(self, url, params=None):
        canonical(url, self.base)
        prepared = requests.Request('GET', url, params=params).prepare().url
        if self.robots and not self.robots.can_fetch(AGENT, prepared):
            raise PermissionError('robots.txt забороняє адресу: ' + prepared)
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=(15, 45), allow_redirects=False)
                if response.status_code in (401, 403, 429):
                    raise PermissionError(f'HTTP {response.status_code}; збір зупинено: {response.url}')
                if 300 <= response.status_code < 400:
                    raise ValueError('Перенаправлення; потрібна перевірка URL: ' + response.url)
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                response.encoding = 'utf-8'
                if any(marker in response.text[:20000].lower() for marker in
                       ('<title>just a moment', 'id="challenge-form"', 'cf-chl-')):
                    raise PermissionError('Отримано перевірку безпеки; збір зупинено')
                return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))

    def json(self, url, params=None):
        response = self.get(url, params)
        if 'json' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('Замість JSON API отримано інший вміст: ' + response.url)
        return response.json(), response


def prepared_body(markup):
    soup = BeautifulSoup(markup or '', 'html.parser')
    media = bool(soup.select('iframe,video,embed'))
    for node in soup.select('script,style,iframe,video,embed,form,figure,figcaption,section.read,ins'):
        node.decompose()
    for node in soup.select('[style]'):
        if node.attrs is None:
            continue
        if re.search(r'color\s*:\s*(?:#fff(?:fff)?\b|white\b)|display\s*:\s*none', node.get('style', ''), re.I):
            node.decompose()
    blocks = [node for node in soup.find_all(BLOCKS) if not node.find_parent(BLOCKS)]
    blocks = [node for node in blocks if clean(node.get_text(' ', strip=True))]
    paragraphs = []
    for node in blocks:
        value = clean(node.get_text(' ', strip=True))
        if re.match(r'^(?:Читайте також|Читай також|Фото\s*:|Скриншот\s*:)', value, re.I):
            continue
        paragraphs.append(value)
    text = '\n\n'.join(paragraphs)
    return soup, blocks, text, media


def links_from(nodes, url):
    links = {}
    for node in nodes:
        for a in node.select('a[href]'):
            target = urljoin(url, a['href'])
            if urlsplit(target).scheme not in ('http', 'https'):
                continue
            if re.search(r'\.(?:jpe?g|png|webp|gif)(?:\?|$)', target, re.I):
                continue
            links.setdefault(target, {'url': target, 'anchor': clean(a.get_text(' ', strip=True)), 'role': 'unreviewed'})
    return list(links.values())


def marker(node):
    text = clean(node.get_text(' ', strip=True))
    match = re.fullmatch(r'(?:В\s*Е\s*Р\s*Д\s*И\s*К\s*Т|ВИСНОВОК)\s*[:：—–-]\s*(.{1,80})', text, re.I)
    if match:
        value = match[1].strip(' .!')
        return value, LABELS.get(re.sub(r'\s+', '', value.casefold()))
    if node.name.startswith('h') and text.strip(' .!:').casefold() in LABELS:
        value = text.strip(' .!:')
        return value, LABELS[value.casefold()]
    return None


def headline(title):
    match = TITLE_PREFIX.match(title)
    if match:
        verdict, claim = match[1], title[match.end():].strip()
        framing = title[:match.end()]
        if verdict.casefold() == 'правда' and not re.search(r'[:.：—–-]', framing):
            return None, None, None
        verdicts = list(re.finditer(r'\b' + VERDICT_WORD + r'\b', framing, re.I))
        if len({v[0].casefold() for v in verdicts}) > 1:
            return clean(framing[verdicts[0].start():verdicts[-1].end()]), None, None
    else:
        match = TITLE_SUFFIX.fullmatch(title)
        if not match:
            return None, None, None
        claim, verdict = match.groups()
    # Питання та заголовки на кшталт «Як спростували...» не є самим твердженням.
    usable = len(claim) >= 30 and not re.match(r'^(?:як|чому|чи|що відомо|скільки)\b', claim, re.I) and not claim.endswith('?')
    return verdict, LABELS[verdict.casefold()], claim if usable else None


def base_record(config, post, title, text, kind, archives, names):
    url = canonical(post['link'], config['base'])
    timestamp = post.get('date_gmt')
    published = timestamp + 'Z' if timestamp and timestamp != '0000-00-00T00:00:00' else None
    return {
        'schema_version': '1.1', 'id': config['source'] + '_' + str(post['id']),
        'source': config['source'], 'record_type': kind, 'url': url,
        'published_at': published, 'collected_at': now(),
        'title_original': title, 'article_text': text, 'claim': None,
        'claim_candidate': None, 'verdict_original': None, 'label': None,
        'label_basis': None, 'review_status': 'pending',
        'categories': names, 'archive_categories': archives,
        'links': [], 'evidence_urls': [], 'duplicate_urls': [],
        'text_hash': sha(clean(text).casefold()),
        'metadata': {'parser_version': '1.0', 'wp_post_id': post['id'],
                     'requires_language_check': True}
    }


def extract_claims(parent, blocks):
    output = {}
    observed = []
    for index, node in enumerate(blocks):
        verdict = marker(node)
        if not verdict:
            continue
        name, label = verdict
        observed.append(name)
        claim = None
        # Шукаємо попередню явно названу тезу, не цитату з наступного спростування.
        for position in range(index - 1, max(-1, index - 5), -1):
            previous = clean(blocks[position].get_text(' ', strip=True))
            if marker(blocks[position]) or re.match(r'^(?:ДОКАЗИ|АРГУМЕНТИ|ВИСНОВКИ)\b', previous, re.I):
                break
            match = THESIS.match(previous)
            if match and match[1]:
                claim = match[1]
                break
            if position == index - 1 and (blocks[position].name == 'blockquote' or re.match(r'^[«“„"]', previous)):
                claim = previous
                break
            if position == index - 1 and previous.count('«') == previous.count('»') == 1:
                quote = re.search(r'«([^«»]{30,5000})»', previous)
                if quote:
                    claim = '«' + quote[1] + '»'
                    break
        if not claim or not (30 <= len(claim) <= 5000):
            continue
        explanation_nodes = []
        for next_node in blocks[index + 1:]:
            value = clean(next_node.get_text(' ', strip=True))
            if marker(next_node) or THESIS.match(value):
                break
            explanation_nodes.append(next_node)
        explanation = '\n\n'.join(clean(n.get_text(' ', strip=True)) for n in explanation_nodes)
        identifier = parent['id'] + '_claim_' + sha(claim + '\n' + name)[:16]
        child = dict(parent)
        child.update({'id': identifier, 'record_type': 'verified_claim',
                      'article_text': explanation, 'claim': claim, 'claim_candidate': claim,
                      'verdict_original': name, 'label': label,
                      'label_basis': 'explicit_claim_verdict', 'text_hash': sha(clean(claim).casefold()),
                      'links': links_from(explanation_nodes, parent['url']), 'duplicate_urls': []})
        child['metadata'] = dict(parent['metadata'], parent_id=parent['id'],
                                 extraction_method='explicit_thesis_before_verdict',
                                 requires_context_review=True, verdict_block_index=index,
                                 content_status='text' if len(explanation) >= 150 else 'short_text')
        # Коротке пояснення не означає коротку цитату: повна стаття є в parent_id.
        child['metadata'].pop('content_status', None)
        output[identifier] = child
    return list(output.values()), observed


def parse_post(config, post, category_info, modes, selected_stream):
    if post.get('content', {}).get('protected'):
        raise ValueError('Закритий текст статті')
    url = canonical(post['link'], config['base'])
    if urlsplit(url).path.startswith(('/en/', '/ru/')) or post.get('lang') in ('en', 'ru'):
        return [], 'non_ukrainian_version'
    ids = set(post.get('categories', []))
    if ids.intersection(modes['exclude']):
        return [], 'excluded_category'
    title = text_of(post.get('title', {}).get('rendered', ''))
    if not title:
        raise ValueError('Порожній заголовок')
    soup, blocks, text, media = prepared_body(post.get('content', {}).get('rendered', ''))
    verdict, label, claim = headline(title)
    is_factcheck = bool(ids.intersection(modes['factcheck'])) or verdict is not None or any(marker(node) for node in blocks)
    names = sorted({text_of(category_info[str(i)]['name']) for i in ids if str(i) in category_info})
    archives = [name for name in ('factcheck', 'news') if ids.intersection(modes[name])]
    if selected_stream not in archives:
        archives.append(selected_stream)
    kind = 'factcheck_article' if is_factcheck else 'news_article'
    parent = base_record(config, post, title, text, kind, archives, names)
    parent['links'] = links_from([soup], url)
    parent['media_type'] = 'mixed' if media else 'text'
    status = 'text' if len(text) >= 150 else 'video_only' if media else 'short_text'
    parent['metadata']['content_status'] = status
    if is_factcheck:
        parent.update({'verdict_original': verdict, 'label': label, 'claim_candidate': claim,
                       'label_basis': 'explicit_title_verdict' if verdict else None})
        children, observed = extract_claims(parent, blocks)
        parent['metadata']['body_verdicts'] = observed
        body_labels = {LABELS.get(re.sub(r'\s+', '', value.casefold())) for value in observed}
        body_labels.discard(None)
        if label and body_labels and (len(body_labels) > 1 or label not in body_labels):
            parent.update({'label': None, 'claim_candidate': None,
                           'review_status': 'pending_verdict_conflict'})
        # Заголовок статті не отримує вердикт однієї з кількох тез у тексті.
        if status != 'text':
            parent['label'] = None
            parent['claim_candidate'] = None
            children = []
        return [parent, *children], None
    if config['source'] != 'gwara' or not ids.intersection(modes['news']):
        return [], 'outside_selected_news'
    if re.search(r'аналіз|аналітик|колонк|інтерв|реклам|партнер|editorial', ' '.join(names), re.I):
        return [], 'non_news_genre'
    if re.search(r'фейк|маніпуляц|спростуван|дайджест|фейкотня', title, re.I):
        return [], 'news_about_factchecking_or_digest'
    parent.update({'label': 'true' if status == 'text' else None,
                   'label_basis': 'source_based', 'claim_candidate': title,
                   'review_status': 'not_individually_verified'})
    parent['metadata']['label_quality'] = 'weak'
    return [parent], None


def category_map(client, db, config):
    cached = db.execute("SELECT value FROM meta WHERE key='categories'").fetchone()
    if cached:
        return json.loads(cached[0])
    categories = []
    page = 1
    while True:
        values, response = client.json(config['base'] + '/wp-json/wp/v2/categories', {'per_page': 100, 'page': page})
        if not isinstance(values, list):
            raise ValueError('API категорій не повернув список')
        categories.extend(values)
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            break
        page += 1
    result = {str(x['id']): {'name': x['name'], 'slug': x['slug'], 'parent': x.get('parent', 0), 'count': x.get('count', 0)} for x in categories}
    with db:
        db.execute("INSERT INTO meta VALUES ('categories',?)", (json.dumps(result, ensure_ascii=False),))
    return result


def mode_ids(categories, config):
    def descendants(roots):
        result = set(roots)
        while True:
            extra = {int(k) for k, v in categories.items() if v['parent'] in result}
            if extra.issubset(result):
                return result
            result.update(extra)
    modes = {}
    for mode in ('factcheck', 'news'):
        roots = {int(k) for k, v in categories.items() if v['slug'].lower() in config[mode + '_slugs']}
        if config['source'] == 'gwara':
            pattern = r'фактчек|факт.?чек' if mode == 'factcheck' else r'^новини$'
            roots.update(int(k) for k, v in categories.items() if re.search(pattern, text_of(v['name']), re.I))
        modes[mode] = descendants(roots)
    modes['exclude'] = descendants({int(k) for k, v in categories.items() if v['slug'].lower() in ('en', 'english', 'ru', 'russian')})
    return modes


def initialize(db, config):
    db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY,status TEXT,error TEXT,url TEXT);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,parent INTEGER,record TEXT);
        CREATE TABLE IF NOT EXISTS streams(name TEXT PRIMARY KEY,page INTEGER DEFAULT 1,total INTEGER,done INTEGER DEFAULT 0);''')
    with db:
        db.execute("INSERT OR IGNORE INTO meta VALUES ('snapshot_before',?)", (now(),))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('source',?)", (config['source'],))
    if db.execute("SELECT value FROM meta WHERE key='source'").fetchone()[0] != config['source']:
        raise ValueError('У цій папці збережено інший сайт; змініть --output')


def store_post(db, config, post, categories, modes, stream):
    records, skipped = parse_post(config, post, categories, modes, stream)
    existing = db.execute('SELECT record FROM records WHERE parent=?', (post['id'],)).fetchall()
    dates = {json.loads(x[0])['id']: json.loads(x[0])['collected_at'] for x in existing}
    with db:
        db.execute('DELETE FROM records WHERE parent=?', (post['id'],))
        for record in records:
            record['collected_at'] = dates.get(record['id'], record['collected_at'])
            db.execute('INSERT INTO records VALUES (?,?,?)', (record['id'], post['id'], json.dumps(record, ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO posts VALUES (?,?,NULL,?)',
                   (post['id'], 'skip:' + skipped if skipped else 'done', post['link']))
    return records, skipped


def collect(client, db, config, args):
    categories = category_map(client, db, config)
    modes = mode_ids(categories, config)
    for mode in args.sources:
        if not modes[mode]:
            names = ', '.join(sorted(v['slug'] for v in categories.values()))
            raise ValueError(f'Не знайдено категорію {mode}. Наявні slug: {names}. Потрібна адаптація структури сайту.')
        selected = [v['name'] for k, v in categories.items() if int(k) in modes[mode]]
        print(f'[{mode}] Категорії: ' + ', '.join(selected), flush=True)
    snapshot = db.execute("SELECT value FROM meta WHERE key='snapshot_before'").fetchone()[0]
    attempts = 0
    if args.retry_errors:
        for identifier, url in db.execute("SELECT id,url FROM posts WHERE status='error'").fetchall():
            if args.max_articles and attempts >= args.max_articles:
                return
            attempts += 1
            try:
                post, _ = client.json(config['base'] + '/wp-json/wp/v2/posts/' + str(identifier), {'_embed': 'wp:term'})
                stream = 'factcheck' if set(post.get('categories', [])).intersection(modes['factcheck']) else 'news'
                if stream not in args.sources:
                    attempts -= 1
                    continue
                store_post(db, config, post, categories, modes, stream)
            except PermissionError:
                raise
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                with db:
                    db.execute('UPDATE posts SET error=? WHERE id=?', (str(exc), identifier))
    for stream in args.sources:
        with db:
            db.execute('INSERT OR IGNORE INTO streams(name) VALUES (?)', (stream,))
        pages_this_run = 0
        while True:
            page, total, done = db.execute('SELECT page,total,done FROM streams WHERE name=?', (stream,)).fetchone()
            if done or (args.max_pages and pages_this_run >= args.max_pages):
                break
            if args.max_articles and attempts >= args.max_articles:
                return
            news_count = sum(1 for (raw,) in db.execute('SELECT record FROM records') if json.loads(raw)['record_type'] == 'news_article')
            if stream == 'news' and args.max_news and news_count >= args.max_news:
                print(f'[news] Досягнуто квоту {args.max_news}.', flush=True)
                break
            params = {'categories': ','.join(map(str, sorted(modes[stream]))), 'per_page': 50,
                      'page': page, 'orderby': 'date', 'order': 'desc', 'before': snapshot, '_embed': 'wp:term'}
            if modes['exclude']:
                params['categories_exclude'] = ','.join(map(str, sorted(modes['exclude'])))
            posts, response = client.json(config['base'] + '/wp-json/wp/v2/posts', params)
            if not isinstance(posts, list):
                raise ValueError('API статей не повернув список')
            total = int(response.headers.get('X-WP-TotalPages', '0'))
            if not posts:
                with db:
                    db.execute('UPDATE streams SET done=1 WHERE name=?', (stream,))
                break
            if total < 1:
                raise ValueError('API не повідомив кількість сторінок')
            with db:
                db.execute('UPDATE streams SET total=? WHERE name=?', (total, stream))
            pages_this_run += 1
            print(f'[{stream}] Сторінка {page}/{total}: {len(posts)} матеріалів', flush=True)
            for post in posts:
                if db.execute('SELECT 1 FROM posts WHERE id=?', (post['id'],)).fetchone():
                    continue
                if args.max_articles and attempts >= args.max_articles:
                    return
                if stream == 'news' and args.max_news and news_count >= args.max_news:
                    return
                attempts += 1
                try:
                    records, skipped = store_post(db, config, post, categories, modes, stream)
                    news_count += sum(r['record_type'] == 'news_article' for r in records)
                    labels = Counter(r['label'] or 'unclassified' for r in records)
                    print(f'[{attempts}] {text_of(post["title"]["rendered"])} | {dict(labels) if records else skipped}', flush=True)
                except (ValueError, KeyError, TypeError) as exc:
                    with db:
                        db.execute('INSERT OR REPLACE INTO posts VALUES (?,\'error\',?,?)', (post['id'], str(exc), post.get('link')))
                    print(f'[{attempts}] Помилка post:{post["id"]}: {exc}', flush=True)
            with db:
                db.execute('UPDATE streams SET page=?,done=? WHERE name=?', (page + 1, int(page >= total), stream))


def export(db, directory):
    unique = {}
    stored = 0
    for (raw,) in db.execute('SELECT record FROM records ORDER BY rowid'):
        r = json.loads(raw)
        stored += 1
        selected_text = r.get('claim') or r.get('claim_candidate') or r['title_original']
        key = (r['record_type'], clean(selected_text).casefold(), r['text_hash'], r['verdict_original'], r['label'])
        if key in unique:
            if r['url'] != unique[key]['url'] and r['url'] not in unique[key]['duplicate_urls']:
                unique[key]['duplicate_urls'].append(r['url'])
        else:
            unique[key] = r
    records = list(unique.values())
    errors = [{'id': 'post:' + str(i), 'url': u, 'error': e} for i, u, e in db.execute("SELECT id,url,error FROM posts WHERE status='error'")]
    streams = {name: {'next_page': page, 'total_pages': total, 'complete': bool(done)}
               for name, page, total, done in db.execute('SELECT name,page,total,done FROM streams')}
    summary = {'stored_records': stored, 'unique_exported': len(records), 'duplicates_merged': stored - len(records),
               'labels': dict(Counter(r['label'] or 'unclassified' for r in records)),
               'record_types': dict(Counter(r['record_type'] for r in records)),
               'claim_labels': dict(Counter(r['label'] or 'unclassified' for r in records if r['record_type'] == 'verified_claim')),
               'headline_labels': dict(Counter(r['label'] or 'unclassified' for r in records if r['record_type'] == 'factcheck_article')),
               'source_based_news': sum(r['record_type'] == 'news_article' and r['label'] == 'true' for r in records),
               'content_status': dict(Counter(r['metadata'].get('content_status', 'claim') for r in records)),
               'body_verdicts': dict(Counter(v for r in records if r['record_type'] == 'factcheck_article' for v in r['metadata'].get('body_verdicts', []))),
               'skipped': dict(db.execute("SELECT status,count(*) FROM posts WHERE status LIKE 'skip:%' GROUP BY status")),
               'errors': len(errors), 'streams': streams,
               'snapshot_before': dict(db.execute('SELECT key,value FROM meta')).get('snapshot_before'),
               'note': 'Сирий корпус. verified_claim — автоматично виділена теза з вердиктом, не обов’язково true. '
                       'Статті та цитати можуть описувати одне твердження. news_article: true за джерелом. '
                       'Неоднозначні вердикти не перетворюються на клас. Підготовку та мовний відбір виконує prepare_claims.py.'}
    destination = directory / 'articles.jsonl'
    temp = destination.with_suffix('.jsonl.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        for r in records:
            stream.write(json.dumps(r, ensure_ascii=False) + '\n')
    temp.replace(destination)
    for name, value in [('summary.json', summary), ('errors.json', errors)]:
        path = directory / name
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('Файл:', destination)


def run(config):
    parser = argparse.ArgumentParser(description='Збір ' + config['name'] + ' у спільний формат JSONL')
    parser.add_argument('--output', type=Path, default=Path('data/raw') / config['source'])
    parser.add_argument('--sources', nargs='+', choices=config['sources'], default=config['sources'])
    parser.add_argument('--max-pages', type=int, default=0, help='Сторінок API на розділ за запуск; 0 — усі')
    parser.add_argument('--max-articles', type=int, default=10, help='Нових спроб за запуск; 0 — усі')
    parser.add_argument('--max-news', type=int, default=2000, help='Квота звичайних новин загалом; 0 — усі')
    parser.add_argument('--delay', type=float, default=2)
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--export-only', action='store_true')
    args = parser.parse_args()
    args.sources = list(dict.fromkeys(args.sources))
    if min(args.max_pages, args.max_articles, args.max_news) < 0:
        parser.error('Ліміти мають бути >= 0')
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / 'state.sqlite3')
    client = None
    ready = False
    try:
        initialize(db, config)
        ready = True
        if args.export_only:
            return
        client = Client(config['base'], args.delay)
        collect(client, db, config, args)
    except KeyboardInterrupt:
        print('\nЗупинено. Прогрес збережено. Повторіть команду для продовження.')
    except (requests.RequestException, PermissionError, ValueError, KeyError, TypeError) as exc:
        print('Збір зупинено:', exc)
    finally:
        if ready:
            export(db, args.output)
        if client:
            client.session.close()
        db.close()
