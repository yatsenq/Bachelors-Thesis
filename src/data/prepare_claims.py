"""Відбір коротких тверджень із JSONL різних джерел. Не змінює сирі дані.

Підтримка: verified_claim, factcheck_article, news_article.
Для news_article потрібні явні label='true' та непорожній label_basis.
Поле source може містити будь-яку назву: окремих правил для сайтів немає.
Мітки не виводяться з назви сайту, suggested_label або відсутності спростування.
Для новин береться claim_candidate або title_original/title/headline; повний
article_text не використовується. Новини мають слабку мітку за джерелом.
Залежність: lingua-language-detector.
"""
import argparse
import hashlib
import html
import json
import re
import random
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from lingua import Language, LanguageDetectorBuilder

LANGUAGE_DETECTOR = LanguageDetectorBuilder.from_languages(
    Language.UKRAINIAN,
    Language.RUSSIAN,
    Language.ENGLISH,
    Language.BELARUSIAN,
    Language.POLISH
).build()

LABELS = {'true', 'false', 'manipulation'}
ORIGINS = ('explicit_verdict_and_quote', 'headline_candidate', 'news_headline')
ORIGIN_PRIORITY = {name: index for index, name in enumerate(ORIGINS)}
PREFIX = re.compile(r'^\s*(?:фотофейк|відеофейк|фейк|неправда|правда|маніпуляція)\s*[:：]\s*', re.I)


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(unicodedata.normalize('NFC', text or ''))).strip()


def normalized(text):
    text = clean(text).casefold().translate(str.maketrans({
        '’': "'",
        'ʼ': "'",
        '`': "'",
        '–': '-',
        '—': '-'
    }))
    text = re.sub(r'[«»“”„"]', '', text)
    text = re.sub(r'(?<!\d)[,.!?;:]+|[,.!?;:]+(?!\d)', ' ', text)
    text = re.sub(r'\s*-\s*', '-', text)
    return re.sub(r'\s+', ' ', text).strip()

CONTEXT_REVIEW_TEXTS = {
    normalized("А вони кажуть, що їх навіть не запитують"):
        "Не зазначено, про кого йдеться та про що запитують.",
    normalized("Гроші в бюджет-2018 на цю інституцію вже заклали"):
        "Не зазначено, про яку інституцію йдеться."
}

def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def text_value(value):
    return clean(value) if isinstance(value, str) else ''


def string_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, str) and x.strip()]
    return []


def url_group(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        return None
    if not parsed.path.strip('/'):
        return None
    # Лише знімаємо фрагмент; параметри URL не відкидаємо довільно.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))

def check_language(text):
    scores = LANGUAGE_DETECTOR.compute_language_confidence_values(text)

    if not scores:
        return "language_uncertain", {}

    values = {
        item.language.name.lower(): round(item.value, 4)
        for item in scores
    }

    best = scores[0]

    if best.value < 0.80:
        return "language_uncertain", values

    if best.language != Language.UKRAINIAN:
        return "language_" + best.language.name.lower(), values

    return None, values

def candidate(record, path, line):
    label = record.get('label')
    kind = record.get('record_type', 'factcheck_article')
    meta = record.get('metadata') or {}
    if not isinstance(meta, dict):
        return None, 'invalid_metadata'
    if kind == 'medical_database_entry':
        return None, 'medical_separate_taxonomy'
    if meta.get('content_status') in ('short_text', 'video_only') or record.get('review_status') == 'excluded_short_content':
        return None, 'short_or_video_only'
    if label not in LABELS:
        verdict = text_value(record.get('verdict_original')) or 'no_explicit_verdict'
        return None, 'unmapped_verdict: ' + verdict.casefold()
    label_basis = text_value(record.get('label_basis'))
    label_quality = 'factcheck_derived'
    if kind == 'verified_claim':
        text = record.get('claim')
        origin = 'explicit_verdict_and_quote'
    elif kind == 'factcheck_article':
        text = record.get('claim_candidate')
        origin = 'headline_candidate'
        # Заголовки не є повністю незалежними від редакційного формулювання.
    elif kind == 'news_article':
        if label != 'true':
            return None, 'news_requires_explicit_true_label'
        if not label_basis:
            return None, 'news_missing_label_basis'
        text = next((record.get(field) for field in
                     ('claim_candidate', 'title_original', 'title', 'headline')
                     if text_value(record.get(field))), None)
        origin = 'news_headline'
        label_quality = 'weak'
        # Не прибираємо вердикт і не перетворюємо його на true-твердження.
        if isinstance(text, str) and PREFIX.match(clean(text)):
            return None, 'news_verdict_style_title'
    else:
        return None, 'unsupported_record_type: ' + str(kind)
    if not isinstance(text, str) or not clean(text):
        return None, 'no_claim_text'
    text = clean(text) if kind == 'news_article' else PREFIX.sub('', clean(text))
    if len(text) < 40:
        return None, 'claim_under_40_characters'
    if len(text) > 5000:
        return None, 'claim_over_5000_characters'
    if re.search(r'(?:\.{3}|…)[\s»”"’\'.!?)]*$', text):
        return None, 'possibly_truncated_claim'
    if re.search(r'<\s*/?\s*[a-zA-Z][^>]*>', text):
        return None, 'html_in_claim'
    record_id = str(record.get('id') or record.get('url') or f'{path.resolve()}:{line}')
    source = str(record.get('source') or path.parent.name)
    parent_id = str(meta.get('parent_id') or record_id)
    related_urls = sorted({u for value in [record.get('url'), record.get('original_url'),
                                          meta.get('parent_url'), *string_list(record.get('duplicate_urls'))]
                           if (u := url_group(value))})
    label_basis = label_basis or 'factcheck_label'
    provenance = {
        'record_id': record_id, 'parent_id': parent_id, 'source': source,
        'url': record.get('url'), 'published_at': record.get('published_at'),
        'verdict_original': record.get('verdict_original'), 'claim_origin': origin,
        'record_type': kind, 'label_basis': label_basis, 'label_quality': label_quality,
        'review_status_original': record.get('review_status'),
        'categories': string_list(record.get('categories')),
        'archive_categories': string_list(record.get('archive_categories')),
        'related_urls': related_urls,
        'raw_file': str(path), 'raw_line': line,
    }
    return {'text': text, 'label': label, 'claim_origin': origin, 'provenance': provenance,
            'label_basis': label_basis, 'label_quality': label_quality,
            'normalization_key': normalized(text)}, None


class Groups:
    def __init__(self):
        self.parents = {}

    def root(self, key):
        self.parents.setdefault(key, key)
        trail = []
        while self.parents[key] != key:
            trail.append(key)
            key = self.parents[key]
        for node in trail:
            self.parents[node] = key
        return key

    def join(self, left, right):
        a, b = self.root(left), self.root(right)
        self.parents[max(a, b)] = min(a, b)


def prepare(paths, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f'Не знайдено вхідний файл: {path}')
        if path.resolve() == (directory / 'claims_candidates.jsonl').resolve():
            raise ValueError('Вихідний файл не може бути вхідним.')
    counters = Counter()
    counters_by_type = Counter()
    excluded = Counter()
    verdicts = Counter()
    entries = {}
    language_review = []
    context_review = []
    for path in paths:
        with path.open(encoding='utf-8-sig') as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f'{path}:{line_number}: очікується JSON-об’єкт')
                counters['raw_records'] += 1
                counters_by_type[str(record.get('record_type', 'factcheck_article'))] += 1
                item, reason = candidate(record, path, line_number)
                if reason:
                    excluded[reason] += 1
                    if record.get('label') not in LABELS:
                        verdicts[text_value(record.get('verdict_original')) or '(немає вердикту)'] += 1
                    continue
                context_reason = CONTEXT_REVIEW_TEXTS.get(item["normalization_key"])

                if context_reason:
                    excluded["context_review_required"] += 1
                    context_review.append({
                        "text": item["text"],
                        "label": item["label"],
                        "reason": context_reason,
                        "provenance": item["provenance"]
                    })
                    continue
                language_reason, language_scores = check_language(item["text"])

                if language_reason:
                    excluded[language_reason] += 1
                    language_review.append({
                        "text": item["text"],
                        "label": item["label"],
                        "reason": language_reason,
                        "language_scores": language_scores,
                        "provenance": item["provenance"]
                    })
                    continue
                counters['eligible_records_before_dedup'] += 1
                entries.setdefault(item['normalization_key'], []).append(item)
    groups = Groups()
    output = []
    conflicts = []
    for key, items in entries.items():
        labels = {x['label'] for x in items}
        if len(labels) > 1:
            conflicts.append({'text': items[0]['text'], 'labels': sorted(labels),
                              'provenance': [x['provenance'] for x in items]})
            counters['conflicting_text_groups'] += 1
            counters['records_in_conflicting_groups'] += len(items)
            continue
        # Якщо є і цитата, і заголовок з однаковим текстом, перевага цитаті.
        best = min(items, key=lambda x: ORIGIN_PRIORITY[x['claim_origin']])
        provenance = [x['provenance'] for x in items]
        parents = sorted({'record:' + x['source'] + ':' + x['parent_id'] for x in provenance}
                         | {'url:' + u for x in provenance for u in x['related_urls']})
        for parent in parents[1:]:
            groups.join(parents[0], parent)
        groups.root(parents[0])
        output.append({'id': 'claim_' + sha(key)[:24], 'text': best['text'], 'label': best['label'],
                       'claim_origin': best['claim_origin'], 'review_status': 'pending',
                       'label_basis': best['label_basis'], 'label_quality': best['label_quality'],
                       'label_bases': sorted({x['label_basis'] for x in items}),
                       'provenance': provenance, '_parent': parents[0]})
        counters['duplicate_records_merged'] += len(items) - 1
    for record in output:
        record['split_group'] = 'group_' + sha(groups.root(record.pop('_parent')))[:20]
    destination = directory / 'claims_candidates.jsonl'
    temporary = destination.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        for record in output:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    temporary.replace(destination)
    # Конфлікти зберігаються лише у звіті; до кандидатів вони не потрапляють.
    samples = {}
    randomizer = random.Random(42)
    for label in sorted(LABELS):
        for origin in ORIGINS:
            pool = [r for r in output if r['label'] == label and r['claim_origin'] == origin]
            if pool:
                samples[label + ':' + origin] = randomizer.sample(pool, min(5, len(pool)))
    report = {
        'input_files': [str(x) for x in paths], **dict(counters),
        'unique_candidates': len(output),
        'labels': dict(Counter(x['label'] for x in output)),
        'claim_origins': dict(Counter(x['claim_origin'] for x in output)),
        'label_quality': dict(Counter(x['label_quality'] for x in output)),
        'primary_label_basis': dict(Counter(x['label_basis'] for x in output)),
        'candidates_by_source': dict(Counter(source for x in output
                                            for source in {p['source'] for p in x['provenance']})),
        'input_records_by_type': dict(counters_by_type),
        'excluded_reasons': dict(excluded), 'unmapped_original_verdicts': dict(verdicts),
        'conflicts': conflicts, 'review_sample': samples,
        'language_review': language_review,
        'context_review': context_review,
        'note': 'Автоматично відібрані короткі кандидати. news_article: true за джерелом '
                '(label_quality=weak), без індивідуального фактчекінгу. Для всіх типів '
                'використано твердження або заголовок, не повний текст. Перефразовані '
                'дублікати ще не усунуто. candidates_by_source рахує всі джерела '
                'об’єднаного запису, тому сума може перевищувати unique_candidates. '
                'Lingua 0.80 — поріг відбору, не гарантія точності визначення мови.',
    }
    report_path = directory / 'preparation_report.json'
    report_temporary = report_path.with_suffix('.tmp')
    report_temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report_temporary.replace(report_path)
    print(json.dumps({k: report[k] for k in ['raw_records', 'unique_candidates', 'labels', 'claim_origins', 'label_quality', 'candidates_by_source', 'excluded_reasons']}, ensure_ascii=False, indent=2))
    print(f'Кандидати: {destination}')
    print(f'Звіт: {directory / "preparation_report.json"}')
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, nargs='+', default=[Path('data/raw/voxcheck/articles.jsonl')])
    parser.add_argument('--output', type=Path, default=Path('data/processed/claims'))
    args = parser.parse_args()
    prepare(args.input, args.output)


if __name__ == '__main__':
    main()

