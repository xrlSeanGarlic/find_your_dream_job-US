"""Claude job search: collect, prefilter, screen, dedupe, queue, record and shortlist.

Workspace layout (all paths relative to --workspace):
  config.json   sources, title lanes, exclusions, budgets
  profile.json  candidate facts with stable IDs + resume versions
  jobs.sqlite3  jobs, reviews, runs
  external_seen.json  identities delivered by another workflow (e.g. Codex) - read-only input

The script never submits applications. Assessment is done by the agent; the script only
stores evidence, applies explicit rule screens and formats the list.
"""
import argparse
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DISPOSITIONS = ('strong', 'stretch', 'hold', 'skip')
RECOMMENDED = ('strong', 'stretch')
VISA_RISK = ('low', 'medium', 'high', 'unknown')
RESUMES = ('A', 'B', 'C', 'custom', '')
REVIEW_DAYS = 7


# ---------------------------------------------------------------- utilities
def dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def digest(v):
    return sha256(dump(v).encode('utf-8')).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def now():
    return datetime.now(timezone.utc)


def s(v, field, required=False):
    if v is None:
        v = ''
    if not isinstance(v, str) or (required and not v.strip()):
        raise ValueError(f'{field} must be {"a nonempty string" if required else "a string"}')
    return v.strip()


def slist(v, field):
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f'{field} must be a list of strings')
    return [s(x, field, True) for x in v]


def identity(url):
    """Canonical posting identity used for dedupe across sources."""
    url = s(url, 'URL', True)
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError(f'Not an HTTP(S) URL: {url}')
    host = parts.hostname.lower().removeprefix('www.')
    path = re.sub(r'/+$', '', parts.path)
    q = dict(parse_qsl(parts.query))
    if host in ('jobs.ashbyhq.com', 'jobs.lever.co'):
        m = re.fullmatch(r'/([^/]+)/([^/]+)(?:/(?:application|apply))?', path)
        if m:
            return f'{host}/{m[1].casefold()}/{m[2].casefold()}'
    if host.endswith('greenhouse.io'):
        m = re.fullmatch(r'/(?:embed/job_app)?([^/]*)/?(?:jobs/)?(\d+)?(?:/apply)?', path)
        m2 = re.search(r'/([^/]+)/jobs/(\d+)', path)
        if m2:
            return f'greenhouse:{m2[1].casefold()}:{m2[2]}'
        if q.get('for') and q.get('token'):
            return f'greenhouse:{q["for"].casefold()}:{q["token"]}'
    if 'gh_jid' in q:  # employer-hosted greenhouse embed
        return f'gh_jid:{q["gh_jid"]}'
    if host.endswith('.myworkdayjobs.com'):
        path = re.sub(r'^/[a-z]{2}-[a-z]{2}/', '/', path.casefold()).removesuffix('/apply')
    if host == 'jobs.smartrecruiters.com':
        m = re.fullmatch(r'/([^/]+)/(\d+)[^/]*', path)
        if m:
            return f'smartrecruiters:{m[1].casefold()}:{m[2]}'
    if host == 'apply.workable.com':
        m = re.search(r'/([^/]+)/j/([A-Za-z0-9]+)', path)
        if m:
            return f'workable:{m[1].casefold()}:{m[2].upper()}'
    keep = sorted((k, v) for k, v in parse_qsl(parts.query)
                  if not k.lower().startswith('utm_') and k.lower() not in
                  {'source', 'ref', 'gh_src', 'src', 'nl', 'fr', 'lever-source', 'lever-origin'})
    return urlunsplit(('https', host, path.casefold() if 'workday' in host else path, urlencode(keep), ''))


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_of(html):
    p = PlainHTML()
    p.feed(unescape(html or ''))
    p.close()
    return '\n'.join(x.strip() for x in ''.join(p.parts).splitlines() if x.strip())


def get_json(url, limit=40_000_000):
    req = Request(url, headers={'User-Agent': 'claude-job-search/0.2', 'Accept': 'application/json'})
    with urlopen(req, timeout=60) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'Response exceeded {limit} bytes: {url}')
    return json.loads(raw)


# ---------------------------------------------------------------- prefilter
def compile_cfg(cfg):
    c = {}
    c['lanes'] = {k: re.compile('|'.join(v), re.I) for k, v in cfg['title_lanes'].items()}
    c['exclude_title'] = re.compile('|'.join(cfg['exclude_title']), re.I)
    c['non_us'] = re.compile('|'.join(cfg['non_us_locations']), re.I)
    c['us'] = re.compile('|'.join(cfg['us_locations']), re.I)
    c['us_codes'] = re.compile(cfg.get('us_state_codes', r'(?!x)x'))  # case-sensitive on purpose
    return c


def lanes_for(title, c):
    return [k for k, rx in c['lanes'].items() if rx.search(title)]


def location_ok(loc, c):
    """True = US/remote-US, False = clearly non-US, None = unknown (kept, flagged)."""
    loc = loc or ''
    if not loc.strip():
        return None
    if c['us'].search(loc) or c['us_codes'].search(loc):
        return True
    if c['non_us'].search(loc):
        return False
    return None


def prefilter(title, location, c):
    """Return (keep, lanes, note). Title/location only; JD screening happens later."""
    if c['exclude_title'].search(title):
        return False, [], 'excluded title'
    lanes = lanes_for(title, c)
    if not lanes:
        return False, [], 'no target lane'
    loc = location_ok(location, c)
    if loc is False:
        return False, lanes, 'non-US location'
    return True, lanes, '' if loc else 'location unknown'


# ---------------------------------------------------------------- screening
# Each rule: (id, severity, regex). severity "block" = explicit mandatory mismatch with
# confirmed candidate facts (F-1 / pending OPT, graduated June 2026, entry level).
# "risk" = needs human reading; never auto-rejects by itself.
SCREEN_RULES = [
    ('citizenship_required', 'block',
     r'\b(?:must|required to|requires?|only)\b[^.\n]{0,60}\b(?:u\.?s\.?|united states)\s+citizen(?:s|ship)?\b'
     r'|\bu\.?s\.?\s+citizenship\s+(?:is\s+)?(?:required|mandatory)'
     r'|\bcitizens?\s+only\b'
     r'|\b(?:green card|permanent resident)\b[^.\n]{0,40}\b(?:required|only)\b'
     r'|\bu\.?s\.?\s+person\b[^.\n]{0,40}\b(?:required|must)\b|\bmust be a u\.?s\.?\s+person\b'),
    ('clearance_required', 'block',
     r'\b(?:active|current|existing)\b[^.\n]{0,30}\b(?:secret|ts/sci|top secret|security)\s+clearance'
     r'|\b(?:secret|ts/sci|top secret|security)\s+clearance\b[^.\n]{0,30}\b(?:required|is required|must)'
     r'|\bability to obtain\b[^.\n]{0,40}\bclearance\b|\bclearance eligib'),
    ('no_opt_or_f1', 'block',
     r'\b(?:not|unable to|cannot|do not|does not)\b[^.\n]{0,40}\b(?:accept|consider|support)\b[^.\n]{0,30}\b(?:opt|cpt|f-?1|student visa)'
     r'|\b(?:opt|cpt|f-?1)\b[^.\n]{0,40}\b(?:not (?:eligible|accepted|considered))'),
    ('enrolled_student_required', 'block',
     r'\bmust be (?:currently )?(?:enrolled|a current(?:ly enrolled)? student)'
     r'|\bcurrently enrolled\b[^.\n]{0,60}\b(?:required|must|program)'
     r'|\breturning to school\b|\bpursuing (?:a|an|your) (?:bachelor|master|phd|degree)[^.\n]{0,40}\b(?:required|must)'),
    ('grad_window_mismatch', 'risk',
     r'\b(?:graduat\w+|degree)\b[^.\n]{0,40}\b(?:between|by|in)\b[^.\n]{0,30}\b(?:2027|2028|december 2026|fall 2026|spring 2027)'),
    ('no_sponsorship', 'risk',
     r'\b(?:not|unable to|cannot|will not|won\'t|does not|do not)\b[^.\n]{0,40}\b(?:sponsor|sponsorship|visa)'
     r'|\bsponsorship\b[^.\n]{0,40}\b(?:not (?:available|offered|provided))'
     r'|\bwithout (?:the need for |requiring )?(?:current or future )?(?:visa )?sponsorship'
     r'|\bmust be (?:legally )?authorized to work\b[^.\n]{0,60}\bwithout\b'),
    ('experience_5plus', 'risk',
     r'\b(?:[5-9]|1[0-9])\+?\s*(?:or more\s+)?years?\b[^.\n]{0,40}\b(?:experience|industry)'),
    ('experience_3_4', 'risk', r'\b[34]\+?\s*(?:-\s*\d+\s*)?years?\b[^.\n]{0,40}\b(?:experience|industry)'),
    ('advanced_degree_required', 'risk',
     r'\b(?:phd|ph\.d|master\'?s)\b[^.\n]{0,30}\b(?:required|is required|must)'),
    ('contract_or_staffing', 'risk',
     r'\bcontract(?:or)?\s+(?:position|role|assignment|basis|job|opportunity|employee|worker|engineer|work)\b'
     r'|\bon a contract\b|\bcontract[- ]to[- ](?:hire|perm)\b|\b(?:1099|c2c|corp[- ]to[- ]corp)\b'
     r'|\btemp(?:orary)?\s+(?:position|role|assignment|employee|job|contract)\b'
     r'|\b\d+[- ](?:month|week)s?\s+(?:contract|assignment|temporary|temp)\b'
     r'|\bstaffing (?:agency|firm|company|partner)\b|\bemployment type:\s*(?:contract|temporary|temp)\b'
     r'|\bour client (?:is|are) (?:seeking|looking|hiring)\b'
     r'|\b(?:job|employment|engagement) type\s*:\s*(?:freelance|independent|contract|temporary|temp)\b'
     r'|\bindependent contract(?:or)?\b|\bfreelancers?\b(?!\s+roles)|\bcontract (?:student )?workers?\b'),
    ('everify_mentioned', 'info', r'\be-?verify\b'),
    ('sponsorship_offered', 'info', r'\b(?:visa sponsorship (?:is )?available|will sponsor|offers? (?:visa )?sponsorship|h-?1b sponsorship available)'),
    ('drivers_license', 'risk', r"\bdriver'?s? licen[cs]e\b"),
]
SCREEN_RX = [(i, sev, re.compile(rx, re.I)) for i, sev, rx in SCREEN_RULES]


# ---------------------------------------------------------------- hard requirements
# Profile-dependent automatic blocks (config hard_requirements). Only sentences that clearly
# *require* more than max_years_required_ok years in every alternative, or an MS/PhD with no
# bachelor's / equivalent / years-or-degree alternative. Preferred wording never blocks.
HARD = {'enabled': True, 'max_years_required_ok': 3, 'block_advanced_degree': True}
_NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
        'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'fifteen': 15, 'twenty': 20}
_N = r'(\d{1,2}|' + '|'.join(_NUM) + r')'
_YEARS_RX = re.compile(_N + r'(?:\s*\(\d{1,2}\))?(?:\s*(?:-|–|to)\s*' + _N + r')?\s*\+?\s*(?:or more\s+)?(?:full[- ]time\s+)?years?\b', re.I)
_REQ_HEAD = re.compile(r"^(?:minimum|basic|required|requirements?|qualifications|must[- ]haves?|you have|about you|"
                       r"what you(?:'ll|’ll| will)? (?:need|bring)|who you are|what we(?:'re|’re| are) looking for)\b", re.I)
_SOFT_HEAD = re.compile(r'^(?:preferred|nice|bonus|how to stand out|stand out|desired|desirable|additional|plus|ideal|we prefer|we(?:\'d|\u2019d| would) love|what would make you stand out|it(?:\'s|\u2019s) a plus)', re.I)
_ANY_HEAD = re.compile(r'^[A-Za-z][\w\s&/\'’,()-]{0,58}:?$')
_SOFT = re.compile(r'\b(?:preferred|preference|a plus|bonus|nice[- ]to[- ]have|ideally|desired|desirable|preferable|highly considered|considered a plus|an advantage|advantageous)\b|~\s*\d|\b(?:approximately|around|roughly)\s+\d', re.I)
_REQ_WORD = re.compile(r'\b(?:required|requires?|must|minimum|at least|mandatory)\b', re.I)
_ADV = re.compile(r"\b(?:master(?:'|’)?s|m\.s\.|ph\.?\s?d\.?|doctor(?:ate|al))\b", re.I)
_DEG_ALT = re.compile(r"\b(?:bachelor|b\.?s\.?|b\.a\.|undergraduate|equivalent)\b", re.I)
_YRS_EXCL = re.compile(r'\b(?:years? (?:of )?(?:age|old)|in the (?:past|last)\b|high school|ged|diploma|substitut|in lieu)', re.I)


def _num(x):
    return int(x) if x.isdigit() else _NUM[x.lower()]


_NEUTRAL_HEAD = re.compile(r"^(?:responsibilities|key responsibilities|what you(?:'ll|\u2019ll| will) do|about (?:us|the|our)|benefits|"
                           r"compensation|perks|the role|role overview|overview|who we are|why (?:join|work)|job description|"
                           r"duties|essential (?:duties|functions)|day[- ]to[- ]day|location|salary|pay|our (?:team|mission)|"
                           r"environmental|physical requirements|working conditions|equal opportunity)", re.I)
_LEVEL_LABEL = re.compile(r"\b(?:[1-5]|i{1,3}|iv|level|senior|sr\.?|junior|jr\.?)\s*$", re.I)


def _sentences(text):
    """Yield (sentence, context, label) where context is 'req', 'soft' or '' and label is any 'X:' prefix."""
    lines = []
    for raw in (text or '').splitlines():
        line = raw.strip().lstrip('#*-\u2022\u00b7 \t').strip()
        if not line:
            continue
        if lines and re.match(r'(?:or|alternatively)\b', line, re.I):
            lines[-1] += ' ' + line  # "...; or Master's degree" continues the previous alternative
        else:
            lines.append(line)
    ctx = ''
    for line in lines:
        head, rest = line, ''
        if ':' in line and len(line.split(':', 1)[0]) <= 50:
            head, rest = line.split(':', 1)
        heading_like = bool(rest) or line.endswith(':') or (len(line) <= 60 and not re.search(r'[.;]$', line))
        if _SOFT_HEAD.match(head) and heading_like and not rest.strip():
            ctx = 'soft'; continue
        if _REQ_HEAD.match(head) and heading_like and not rest.strip():
            ctx = 'req'; continue
        if _NEUTRAL_HEAD.match(head) and heading_like and not rest.strip():
            ctx = ''; continue
        lctx = 'soft' if _SOFT_HEAD.match(head) and rest else ('req' if _REQ_HEAD.match(head) and rest else ctx)
        label = head.strip() if rest else ''
        body = rest.strip() if (rest and (_REQ_HEAD.match(head) or _SOFT_HEAD.match(head))) else line
        units = []  # [unit text, [raw sentences]]
        for sent in re.split(r'(?<=\.)\s+(?=[A-Z])', body):
            if units and re.match(r'(?:or|alternatively)\b', sent, re.I):
                units[-1][0] += ' ' + sent  # keep alternatives with the requirement they replace
                units[-1][1].append(sent.strip())
            elif sent.strip():
                units.append([sent.strip(), [sent.strip()]])
        for unit, parts in units:
            yield unit, lctx, label, parts


def hard_mismatches(text):
    if not HARD.get('enabled', True) or not text:
        return []
    max_ok = HARD.get('max_years_required_ok', 3)
    hits, seen = [], set()
    for unit, ctx, label, parts in _sentences(text):
        if ctx == 'soft':
            continue
        # years: judged sentence by sentence; a separate "Alternatively, a Master's + N years" sentence
        # is an advanced-degree path this profile cannot use, so it does not lower the bar
        for sent in parts:
            if _SOFT.search(sent) or not (ctx == 'req' or _REQ_WORD.search(sent)):
                continue
            ev = ' '.join(sent.split())[:300]
            if 'hard_years_required' in seen or not re.search(r'experience|industry', sent, re.I) or _YRS_EXCL.search(sent):
                continue
            mins = [_num(m.group(1)) for m in _YEARS_RX.finditer(sent)]
            # "BS/BA degree or 4 years" is a bachelor's path; an MS/PhD alternative does not help this profile
            alt_degree = len(mins) < 2 and re.search(
                r"(?:degree|bachelor\S*|\bb\.?[sa]\.?(?:/b\.?a\.?)?)\s+or\s+(?:\w+\s+){0,3}" + _N + r"\b"
                r"|years?\b[^.]{0,60}\bor\s+(?:a\s+|an\s+)?(?:bachelor|undergraduate|\bb\.?[sa]\b)", sent, re.I)
            level = label and _LEVEL_LABEL.search(label)  # multi-level postings ("Engineer 3: five years")
            if mins and min(mins) > max_ok and not alt_degree and not level:
                hits.append({'rule': 'hard_years_required', 'severity': 'block', 'evidence': ev})
                seen.add('hard_years_required')
        sent = unit
        # "(preferable)" after PhD qualifies only that option; the MS path is still the minimum
        if _SOFT.search(re.sub(r'\((?:preferabl[ey]|preferred)\)', '', sent, flags=re.I)) \
                or not (ctx == 'req' or _REQ_WORD.search(sent)):
            continue
        ev = ' '.join(sent.split())[:300]
        # advanced degree
        if HARD.get('block_advanced_degree', True) and 'hard_degree_required' not in seen and _ADV.search(sent) \
                and not _DEG_ALT.search(sent) \
                and not re.search(r'\b(?:part of|during|while (?:pursuing|completing))\s+(?:a|an|your)?\s*' + _ADV.pattern, sent, re.I) \
                and not re.search(r'(?:years?|experience)[^.]{0,80}\bor\b[^.]{0,40}' + _ADV.pattern, sent, re.I) \
                and not re.search(_ADV.pattern + r'[^.]{0,80}\bor\b[^.]{0,60}(?:years?|experience|record)', sent, re.I):
            hits.append({'rule': 'hard_degree_required', 'severity': 'block', 'evidence': ev})
            seen.add('hard_degree_required')
    return hits


# Sentences that say F-1/OPT holders do NOT need sponsorship are not a "no sponsorship" signal.
_NS_FP = re.compile(r'[^\n]{0,80}\b(?:F-?1|OPT|CPT)\b[^\n]{0,150}\b(?:have work authori[sz]ation|are authori[sz]ed to work)'
                    r'[^\n]{0,80}\bdo(?:es)? not require\b[^.\n]*', re.I)
_CLR = re.compile(r'\bts\s*/\s*sci\b|\btop secret\b|\bpolygraph\b|\bfully cleared\b'
                  r'|\b(?:secret|security)\s+clearance\b', re.I)
_CLR_NEG = re.compile(r'\b(?:no|not|without)\b[^.\n]{0,30}\b(?:clearance|cleared)\b'
                      r'|\bclearance\W{0,3}(?:is )?(?:not required|not needed|none|n/a)\b', re.I)
_OPT_WORD = re.compile(r'\b(?:F-?1|OPT|CPT|I-?20|I-?983)\b|\btraining plans?\b')
_OPT_NEG = re.compile(r"\b(?:not in a position|not able|unable|cannot|can not|will not|won't|do(?:es)? not|no)\b"
                      r"[^\n]{0,80}\b(?:sponsor\w*|support|offer)\b", re.I)
_OPT_INELIG = re.compile(r'\b(?:not eligible|ineligible|will not be considered|unable to consider|cannot consider|'
                         r'not able to consider)\b', re.I)
_OPT_POS = re.compile(r'\b(?:welcome|encouraged to apply|are eligible|is eligible|can (?:be )?consider|will consider|'
                      r'accept(?:s|ed)?\s+(?:F-?1|OPT|CPT))', re.I)


def _extra_blocks(text):
    """Clearance and 'no F-1/OPT' wording the single-regex rules miss; sentence/heading aware."""
    hits = []
    for unit, ctx, label, parts in _sentences(text):
        if ctx == 'soft' or _SOFT.search(unit) or re.search(r'\b(?:bonus|stand out)\b', label or '', re.I):
            continue
        if _CLR.search(unit) and not _CLR_NEG.search(unit) and (len(unit) > 25 or ctx == 'req'):
            hits.append({'rule': 'clearance_required', 'severity': 'block', 'evidence': ' '.join(unit.split())[:300]})
            break
    for line in (text or '').splitlines():
        if not _OPT_WORD.search(line) or _OPT_POS.search(line):
            continue
        neg = _OPT_NEG.search(line)
        if (neg and _OPT_WORD.search(line, neg.end())) or _OPT_INELIG.search(line):
            words = [m for m in _OPT_WORD.finditer(line)]
            stem_only = all(m.group(0) == 'OPT' and line[max(0, m.start() - 5):m.start()].upper() == 'STEM ' for m in words)
            # "no STEM OPT extension" still allows the initial 12-month OPT: high risk, not a block
            hits.append({'rule': 'no_stem_opt' if stem_only else 'no_opt_or_f1',
                         'severity': 'risk' if stem_only else 'block', 'evidence': ' '.join(line.split())[:300]})
            break
    return hits


def screen(text):
    hits = []
    for rid, sev, rx in SCREEN_RX:
        if rid == 'no_sponsorship':
            m = rx.search(_NS_FP.sub(' ', text or ''))
            if m:
                t = _NS_FP.sub(' ', text or '')
                a, b = max(0, m.start() - 80), min(len(t), m.end() + 80)
                hits.append({'rule': rid, 'severity': sev, 'evidence': ' '.join(t[a:b].split())})
            continue
        m = rx.search(text or '')
        if m and rid == 'clearance_required':
            # "No security clearance required" is the opposite of a requirement
            start = max(text.rfind('.', 0, m.start()), text.rfind('\n', 0, m.start())) + 1
            if _CLR_NEG.search(text[start:m.end() + 40]):
                m = None
        if m:
            a, b = max(0, m.start() - 80), min(len(text), m.end() + 80)
            hits.append({'rule': rid, 'severity': sev, 'evidence': ' '.join(text[a:b].split())})
    have = {h['rule'] for h in hits}
    hits += [h for h in _extra_blocks(text or '') if h['rule'] not in have]
    return hits + hard_mismatches(text or '')


def visa_risk_from(hits):
    ids = {h['rule'] for h in hits}
    if ids & {'citizenship_required', 'clearance_required', 'no_opt_or_f1'}:
        return 'high'
    if ids & {'no_sponsorship', 'no_stem_opt'}:
        return 'high'
    if 'contract_or_staffing' in ids:
        return 'medium'
    if 'sponsorship_offered' in ids or 'everify_mentioned' in ids:
        return 'low'
    return 'unknown'


# ---------------------------------------------------------------- sources
def rec(company, title, url, description='', location='', apply_url='', posted='', source='manual', status='listed', ext=None):
    return dict(company=company, title=title, listing_url=url, description=description or '',
                location=location or '', apply_url=apply_url or '', posted=posted or '',
                source=source, status=status, ext=ext or {})


def fetch_board(provider, slug, company, c, get=get_json):
    """Return (records, seen_count). Records are already title/location-prefiltered."""
    out, seen = [], 0
    if provider == 'greenhouse':
        for it in get(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true')['jobs']:
            seen += 1
            loc = (it.get('location') or {}).get('name') or ''
            if prefilter(it['title'], loc, c)[0]:
                out.append(rec(company, it['title'], it['absolute_url'], text_of(it.get('content')), loc,
                               it['absolute_url'], it.get('first_published') or it.get('updated_at') or '', f'greenhouse:{slug}'))
    elif provider == 'ashby':
        for it in get(f'https://api.ashbyhq.com/posting-api/job-board/{slug}')['jobs']:
            seen += 1
            if it.get('isListed') is False:
                continue
            loc = ' / '.join([it.get('location') or ''] + [x.get('location', '') for x in it.get('secondaryLocations') or []])
            if it.get('isRemote'):
                loc += ' / Remote'
            if prefilter(it['title'], loc, c)[0]:
                out.append(rec(company, it['title'], it['jobUrl'], it.get('descriptionPlain') or text_of(it.get('descriptionHtml')),
                               loc, it.get('applyUrl') or it['jobUrl'], it.get('publishedAt') or '', f'ashby:{slug}'))
    elif provider == 'lever':
        for off in range(0, 3000, 100):
            page = get(f'https://api.lever.co/v0/postings/{slug}?mode=json&limit=100&skip={off}')
            for it in page:
                seen += 1
                cat = it.get('categories') or {}
                loc = ' / '.join([cat.get('location') or ''] + (cat.get('allLocations') or []))
                if not prefilter(it['text'], loc, c)[0]:
                    continue
                desc = '\n'.join([it.get('descriptionPlain') or text_of(it.get('description'))] +
                                 [f"{g.get('text', '')}\n{text_of(g.get('content'))}" for g in it.get('lists') or []] +
                                 [it.get('additionalPlain') or text_of(it.get('additional'))]).strip()
                posted = datetime.fromtimestamp(it['createdAt'] / 1000, timezone.utc).isoformat() if it.get('createdAt') else ''
                out.append(rec(company, it['text'], it['hostedUrl'], desc, loc, it.get('applyUrl') or '', posted, f'lever:{slug}'))
            if len(page) < 100:
                break
    elif provider == 'smartrecruiters':
        for off in range(0, 2000, 100):
            page = get(f'https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={off}')
            for it in page.get('content', []):
                seen += 1
                L = it.get('location') or {}
                loc = ', '.join(x for x in (L.get('city'), L.get('region'), L.get('country')) if x)
                if L.get('remote'):
                    loc += ' / Remote'
                if not prefilter(it['name'], loc, c)[0]:
                    continue
                url = f"https://jobs.smartrecruiters.com/{slug}/{it['id']}"
                try:
                    d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{it['id']}")
                    sec = (d.get('jobAd') or {}).get('sections') or {}
                    desc = '\n'.join(text_of((sec.get(k) or {}).get('text')) for k in
                                     ('companyDescription', 'jobDescription', 'qualifications', 'additionalInformation'))
                    url = d.get('postingUrl') or url
                    apply = d.get('applyUrl') or url
                except Exception:
                    desc, apply = '', url
                out.append(rec(company, it['name'], url, desc, loc, apply, it.get('releasedDate') or '', f'smartrecruiters:{slug}'))
            if off + 100 >= page.get('totalFound', 0):
                break
    else:
        raise ValueError(f'Unknown provider {provider}')
    return out, seen


def fetch_simplify(cfg, c, get=get_json):
    sc = cfg['simplify']
    data = get(sc['url'])
    cutoff = now().timestamp() - sc.get('max_age_days', 14) * 86400
    out, seen = [], 0
    for it in data:
        if not (it.get('active') and it.get('is_visible', True)):
            continue
        seen += 1
        if it.get('sponsorship') in sc.get('skip_sponsorship', []):
            continue
        if (it.get('date_posted') or 0) < cutoff:
            continue
        loc = ' / '.join(it.get('locations') or [])
        if not prefilter(it.get('title', ''), loc, c)[0]:
            continue
        posted = datetime.fromtimestamp(it['date_posted'], timezone.utc).isoformat()
        out.append(rec(it.get('company_name', ''), it.get('title', ''), it['url'], '', loc, it['url'], posted,
                       'simplify', 'listed', {'simplify_id': it.get('id'), 'sponsorship': it.get('sponsorship'),
                                               'degrees': it.get('degrees')}))
    return out, seen


def get_text(url, limit=5_000_000):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; claude-job-search/0.3)',
                                'Accept': 'text/html,application/xhtml+xml'})
    with urlopen(req, timeout=60) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'Response exceeded {limit} bytes: {url}')
    return raw.decode('utf-8', errors='replace')


class _Section(HTMLParser):
    """Collect the inner HTML-ish text of the first element with a given id (div nesting aware)."""
    def __init__(self, want_id):
        super().__init__(convert_charrefs=True)
        self.want, self.depth, self.tag, self.parts, self.done = want_id, 0, None, [], False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth:
            if tag == self.tag:
                self.depth += 1
            self.parts.append(self.get_starttag_text() or '')
        elif dict(attrs).get('id') == self.want:
            self.tag, self.depth = tag, 1

    def handle_endtag(self, tag):
        if self.depth and not self.done:
            if tag == self.tag:
                self.depth -= 1
                if not self.depth:
                    self.done = True
                    return
            self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        if self.depth and not self.done:
            self.parts.append(data.replace('<', '&lt;'))


def html_section(html, want_id):
    p = _Section(want_id)
    p.feed(html or '')
    return text_of(''.join(p.parts))


def ld_job_posting(html):
    for m in re.finditer(r'(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html or ''):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:  # some ATSs (Jobvite) emit malformed JSON-LD; salvage the description string
            if '"JobPosting"' not in m.group(1):
                continue
            dm = re.search(r'"description"\s*:\s*("(?:[^"\\]|\\.)*")', m.group(1), re.S)
            if not dm:
                continue
            try:
                return {'@type': 'JobPosting', 'description': json.loads(dm.group(1))}
            except ValueError:
                continue
        for x in d if isinstance(d, list) else d.get('@graph', [d]) if isinstance(d, dict) else []:
            if isinstance(x, dict) and x.get('@type') == 'JobPosting':
                return x
    return None


def next_data(html):
    m = re.search(r'(?s)<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or '')
    return json.loads(m.group(1)) if m else None


WORKDAY_RX = re.compile(r'https?://(?P<host>(?P<tenant>[^./]+)\.(?:wd\d+\.)?myworkdayjobs\.com)'
                        r'(?:/[a-z]{2}-[A-Za-z]{2})?/(?P<site>[^/?#]+)/job/(?P<rest>[^?#]+?)(?:/apply)?/?(?:[?#].*)?$', re.I)
ORACLE_RX = re.compile(r'https?://(?P<host>[^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/'
                       r'(?P<site>[^/?#]+)/(?:job|requisitions/preview)/(?P<id>\d+)', re.I)
BAMBOO_RX = re.compile(r'https?://(?P<host>[^./]+\.bamboohr\.com)/(?:careers|jobs)/(?:view\.php\?id=)?(?P<id>\d+)', re.I)
GH_JID_RX = re.compile(r'[?&]gh_jid=(\d+)')
HTML_ATS_RX = re.compile(r'https?://(?:[^/]+\.)?(?:jobvite\.com|icims\.com|applytojob\.com|ats\.rippling\.com)/', re.I)


def polite(fn, gap=1.5, retries=3, sleep=time.sleep, clock=time.monotonic):
    """Wrap a fetcher: >= gap seconds between calls to the same rate-limit group, back off on HTTP 429."""
    last = {}

    def group(url):
        h = (urlsplit(url).hostname or '').lower()
        m = re.search(r'(wd\d+)\.myworkdayjobs\.com$', h)
        return m[1] if m else h

    def call(url):
        g = group(url)
        for attempt in range(retries + 1):
            wait = last.get(g, -1e9) + gap - clock()
            if wait > 0:
                sleep(wait)
            last[g] = clock()
            try:
                return fn(url)
            except HTTPError as e:
                if e.code != 429 or attempt == retries:
                    raise
                sleep(5 * 3 ** attempt)
    return call


def workday_api(url):
    m = WORKDAY_RX.match(url or '')
    if not m:
        return ''
    return f"https://{m['host']}/wday/cxs/{m['tenant']}/{m['site']}/job/{m['rest']}"


def oracle_api(url):
    m = ORACLE_RX.match(url or '')
    if not m:
        return ''
    return (f"https://{m['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{m['id']}%22,siteNumber={m['site']}")


def api_resolvable(url):
    try:
        i = identity(url)
    except ValueError:
        return False
    if re.match(r'(?:greenhouse:|jobs\.lever\.co/|jobs\.ashbyhq\.com/|smartrecruiters:)', i):
        return True
    return bool(workday_api(url) or oracle_api(url) or BAMBOO_RX.match(url) or HTML_ATS_RX.match(url)
                or GH_JID_RX.search(url))


def resolve_ext(job, get=get_json, get_html=get_text):
    """Like resolve_jd but for non-classic ATSs; returns {'description', 'status'?} or {}.
    Tries listing_url first (that is where the posting lives), then apply_url."""
    for u in [x for x in (job.get('listing_url'), job.get('apply_url')) if x]:
        gm = GH_JID_RX.search(u)
        if gm:  # employer-hosted Greenhouse embed: guess the board slug from the domain
            host = (urlsplit(u).hostname or '').lower().removeprefix('www.').removeprefix('careers.')
            slug = host.split('.')[0]
            try:
                d = text_of((get(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gm[1]}') or {}).get('content'))
            except HTTPError:
                d = ''
            if d:
                return {'description': d}
        api = workday_api(u)
        if api:
            info = (get(api) or {}).get('jobPostingInfo') or {}
            d = text_of(info.get('jobDescription'))
            if info.get('canApply') is False:
                return {'description': d, 'status': 'closed'}
            if d:
                return {'description': d}
        api = oracle_api(u)
        if api:
            items = (get(api) or {}).get('items') or []
            if not items:
                return {'description': '', 'status': 'closed'}
            if items:
                it = items[0]
                d = '\n'.join(text_of(it.get(k)) for k in ('ExternalDescriptionStr', 'ExternalResponsibilitiesStr',
                                                           'ExternalQualificationsStr', 'CorporateDescriptionStr')
                              if it.get(k)).strip()
                if d:
                    return {'description': d}
        m = BAMBOO_RX.match(u)
        if m:
            jo = ((get(f"https://{m['host']}/careers/{m['id']}/detail") or {}).get('result') or {}).get('jobOpening') or {}
            d = text_of(jo.get('description'))
            if d:
                return {'description': d}
        if HTML_ATS_RX.match(u):
            html = get_html(u)
            if 'ats.rippling.com' in u:
                jp = (((next_data(html) or {}).get('props') or {}).get('pageProps') or {}).get('apiData', {}).get('jobPost') or {}
                desc = jp.get('description') or {}
                d = '\n'.join(text_of(desc.get(k)) for k in ('role', 'company') if desc.get(k)).strip() \
                    if isinstance(desc, dict) else text_of(desc)
                if d:
                    return {'description': d}
            jp = ld_job_posting(html)
            if jp and jp.get('description'):
                return {'description': text_of(jp['description'])}
            if 'applytojob.com' in u:
                d = html_section(html, 'job-description')
                if d:
                    return {'description': d}
    return {}


def resolve_jd(job, get=get_json):
    """Try to fill a missing description through a public ATS API. Returns description or ''."""
    u = job['apply_url'] or job['listing_url']
    ident = identity(u)
    m = re.fullmatch(r'greenhouse:([^:]+):(\d+)', ident)
    if m:
        d = get(f'https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}')
        return text_of(d.get('content'))
    m = re.fullmatch(r'jobs\.lever\.co/([^/]+)/([^/]+)', ident)
    if m:
        it = get(f'https://api.lever.co/v0/postings/{m[1]}/{m[2]}')
        return '\n'.join([it.get('descriptionPlain') or text_of(it.get('description'))] +
                         [f"{g.get('text', '')}\n{text_of(g.get('content'))}" for g in it.get('lists') or []] +
                         [it.get('additionalPlain') or '']).strip()
    m = re.fullmatch(r'jobs\.ashbyhq\.com/([^/]+)/([^/]+)', ident)
    if m:
        for it in get(f'https://api.ashbyhq.com/posting-api/job-board/{m[1]}')['jobs']:
            if it.get('id', '').casefold() == m[2] or m[2] in (it.get('jobUrl') or '').casefold():
                return it.get('descriptionPlain') or text_of(it.get('descriptionHtml'))
    m = re.fullmatch(r'smartrecruiters:([^:]+):(\d+)', ident)
    if m:
        d = get(f'https://api.smartrecruiters.com/v1/companies/{m[1]}/postings/{m[2]}')
        sec = (d.get('jobAd') or {}).get('sections') or {}
        return '\n'.join(text_of((sec.get(k) or {}).get('text')) for k in ('jobDescription', 'qualifications', 'additionalInformation'))
    return ''


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, payload TEXT NOT NULL, job_sha TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  lanes TEXT NOT NULL DEFAULT '[]', screen TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'new'  -- new | blocked | filtered | external_seen | applied | closed
);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, job_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS external (alias TEXT PRIMARY KEY, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, job_sha TEXT NOT NULL, profile_sha TEXT NOT NULL,
  reviewed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shortlisted (id TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(id, day));
CREATE TABLE IF NOT EXISTS runs (at TEXT NOT NULL, command TEXT NOT NULL, result TEXT NOT NULL);
"""


def open_db(ws):
    ws.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ws / 'jobs.sqlite3')
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_run(conn, command, result):
    with conn:
        conn.execute('INSERT INTO runs VALUES (?,?,?)', (now().isoformat(), command, dump(result)))


def job_sha(job):
    return digest({k: job[k] for k in ('company', 'title', 'description', 'location', 'status')} |
                  {'apply': identity(job['apply_url']) if job['apply_url'] else ''})


def store(conn, records, c, authoritative_source=None):
    counts = dict(new=0, changed=0, unchanged=0, merged=0, no_longer_listed=0, blocked=0)
    seen_ids = set()
    t = now().isoformat()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for r in records:
            key = identity(r['listing_url'])
            alias_ids = {key} | ({identity(r['apply_url'])} if r['apply_url'] else set())
            existing = find_existing(conn, alias_ids)
            jid = existing or key
            seen_ids.add(jid)
            prev = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if prev:
                old = json.loads(prev['payload'])
                # keep the richer description and union sources
                if len(old.get('description', '')) > len(r['description']):
                    r = r | {'description': old['description']}
                r['sources'] = sorted(set(old.get('sources', [old.get('source', '')])) | {r['source']})
                r['ext'] = old.get('ext', {}) | r.get('ext', {})
                if existing and existing != key:
                    counts['merged'] += 1
            else:
                r['sources'] = [r['source']]
            sha = job_sha(r)
            lanes = lanes_for(r['title'], c)
            hits = screen(r['description']) if r['description'] else []
            state = prev['state'] if prev else 'new'
            if state in ('new', 'blocked'):
                state = 'blocked' if any(h['severity'] == 'block' for h in hits) else 'new'
            if prev and prev['job_sha'] == sha:
                conn.execute('UPDATE jobs SET last_seen=?, payload=? WHERE id=?', (t, dump(r), jid))
                counts['unchanged'] += 1
            else:
                conn.execute("""INSERT INTO jobs (id,payload,job_sha,first_seen,last_seen,lanes,screen,state)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                    job_sha=excluded.job_sha, last_seen=excluded.last_seen, lanes=excluded.lanes,
                    screen=excluded.screen, state=excluded.state""",
                             (jid, dump(r), sha, t, t, dump(lanes), dump(hits), state))
                counts['changed' if prev else 'new'] += 1
                if state == 'blocked' and not (prev and prev['state'] == 'blocked'):
                    counts['blocked'] += 1
            for a in alias_ids:
                conn.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (a, jid))
        if authoritative_source:
            for row in conn.execute("SELECT id,payload FROM jobs WHERE state NOT IN ('closed','applied')").fetchall():
                j = json.loads(row['payload'])
                if j.get('source') == authoritative_source and row['id'] not in seen_ids and j['status'] == 'listed':
                    j['status'] = 'unknown'
                    conn.execute('UPDATE jobs SET payload=?, job_sha=? WHERE id=?', (dump(j), job_sha(j), row['id']))
                    counts['no_longer_listed'] += 1
    return counts


def import_external(conn, path):
    """Mark jobs already delivered by another workflow. Accepts the Codex ledger format
    ({"items": [{identity, apply_url, verified_aliases, ...}]}), Codex special exclusions,
    or a plain list of URLs. Stored separately so later collections are matched too."""
    data = load(path)
    items = data.get('items', data) if isinstance(data, dict) else data
    ids = set()
    for it in items:
        urls = [it] if isinstance(it, str) else (
            [it.get(k) for k in ('apply_url', 'listing_url', 'platform_listing_url')] + list(it.get('verified_aliases') or []))
        for u in urls:
            if u:
                try:
                    ids.add(identity(u))
                except ValueError:
                    pass
        if isinstance(it, dict) and it.get('identity'):
            ids.add(it['identity'])
            try:
                ids.add(identity('https://' + it['identity']) if '/' in it['identity'] and ':' not in it['identity'] else it['identity'])
            except ValueError:
                pass
    with conn:
        conn.executemany('INSERT OR IGNORE INTO external VALUES (?,?)', [(i, str(path)) for i in ids])
    return {'external_identities': len(ids), 'marked_external_seen': mark_external(conn)}


def refilter(conn, c):
    """Re-apply current title/location prefilter and lanes to open jobs after a config change."""
    n = 0
    with conn:
        for row in conn.execute("SELECT id,payload,lanes,state FROM jobs WHERE state IN ('new','blocked','filtered')").fetchall():
            j = json.loads(row['payload'])
            keep, lanes, _ = prefilter(j['title'], j['location'], c)
            if not keep and row['state'] != 'filtered':
                conn.execute("UPDATE jobs SET state='filtered' WHERE id=?", (row['id'],))
                n += 1
            elif keep and row['state'] == 'filtered':
                conn.execute("UPDATE jobs SET state='new' WHERE id=?", (row['id'],))
            if keep and dump(lanes) != row['lanes']:
                conn.execute('UPDATE jobs SET lanes=? WHERE id=?', (dump(lanes), row['id']))
    return n


def mark_external(conn):
    with conn:
        return conn.execute("""UPDATE jobs SET state='external_seen' WHERE state IN ('new','blocked') AND id IN
            (SELECT a.job_id FROM aliases a JOIN external e ON e.alias=a.alias)""").rowcount


def find_existing(conn, ids):
    for i in ids:
        row = conn.execute('SELECT job_id FROM aliases WHERE alias=?', (i,)).fetchone()
        if row:
            return row['job_id']
    return None


# ---------------------------------------------------------------- profile / queue / record
def profile_at(ws):
    p = load(ws / 'profile.json')
    if not isinstance(p.get('facts'), dict) or not p['facts']:
        raise ValueError('profile.json needs facts with stable IDs')
    return p


def is_current(row, psha):
    cutoff = (now() - timedelta(days=REVIEW_DAYS)).isoformat()
    return (row['review_sha'] == row['job_sha'] and row['profile_sha'] == psha and row['reviewed_at'] >= cutoff)


def rows(conn, where="j.state='new'"):
    return conn.execute(f"""SELECT j.*, r.job_sha AS review_sha, r.profile_sha, r.reviewed_at, r.payload AS review
        FROM jobs j LEFT JOIN reviews r ON r.id=j.id WHERE {where} ORDER BY j.first_seen DESC, j.id""").fetchall()


def lane_ranks(cfg):
    """lane_priority entries are lanes or lists of equally ranked lanes: [["B", "A"], "C"] -> B=A=0, C=1."""
    rank = {}
    for i, tier in enumerate(cfg.get('lane_priority', ['C', 'B', 'A'])):
        for lane in ([tier] if isinstance(tier, str) else tier):
            rank[lane] = i
    return rank


def review_plan(cfg):
    """Per-lane review quota for one run (config budgets.review_plan); C keeps a guaranteed minimum."""
    b = cfg.get('budgets', {})
    plan = dict(b.get('review_plan') or {})
    total = b.get('max_jd_reviews_per_run', 60)
    if not plan:
        return {'total': total, 'lanes': {}}
    if sum(plan.values()) > total:
        raise ValueError(f'review_plan {plan} exceeds max_jd_reviews_per_run {total}')
    return {'total': total, 'lanes': plan, 'extra_if_below_min': b.get('extra_reviews_if_below_min', 0)}


def rescreen(conn, profile):
    """Re-run JD screening (incl. hard blocks) on stored jobs without a current review."""
    psha = digest(profile)
    out = dict(rescreened=0, newly_blocked=0, unblocked=0, kept_reviewed=0)
    with conn:
        for row in rows(conn, "j.state IN ('new','blocked')"):
            j = json.loads(row['payload'])
            if row['review_sha'] and is_current(row, psha):
                out['kept_reviewed'] += 1
                continue
            hits = screen(j.get('description', '')) if j.get('description') else []
            state = 'blocked' if any(h['severity'] == 'block' for h in hits) else 'new'
            if state != row['state']:
                out['newly_blocked' if state == 'blocked' else 'unblocked'] += 1
            conn.execute('UPDATE jobs SET screen=?, state=? WHERE id=?', (dump(hits), state, row['id']))
            out['rescreened'] += 1
    return out


def queue(conn, profile, cfg, limit, per_company, lane=None, need_jd=False):
    psha = digest(profile)
    rank = lane_ranks(cfg)
    cands = []
    for row in rows(conn):
        j = json.loads(row['payload'])
        if j['status'] == 'closed' or is_current(row, psha):
            continue
        lanes = json.loads(row['lanes'])
        if lane and lane not in lanes:
            continue
        if need_jd and j['description']:
            continue
        if not need_jd and not j['description']:
            continue
        hits = json.loads(row['screen'])
        risk = sum(h['severity'] == 'risk' for h in hits)
        best = min((rank[l] for l in lanes if l in rank), default=len(rank))
        cands.append(((best, risk), row['first_seen'], row['id'], j, lanes, hits, row['job_sha']))
    cands.sort(key=lambda x: x[3].get('posted') or '', reverse=True)  # newest first
    cands.sort(key=lambda x: x[0])  # then lane priority, fewer risk flags (stable)
    by_co = {}
    for cnd in cands:
        by_co.setdefault(cnd[3]['company'].casefold(), deque()).append(cnd)
    ring, out, taken = deque(by_co.values()), [], {}
    while ring and len(out) < limit:
        q = ring.popleft()
        cnd = q.popleft()
        co = cnd[3]['company'].casefold()
        if taken.get(co, 0) < per_company:
            taken[co] = taken.get(co, 0) + 1
            _, _, jid, j, lanes, hits, sha = cnd
            item = {'job_id': jid, 'job_sha256': sha, 'company': j['company'], 'title': j['title'],
                    'location': j['location'], 'listing_url': j['listing_url'], 'apply_url': j['apply_url'],
                    'posted': j['posted'], 'sources': j.get('sources'), 'lanes': lanes, 'screen': hits}
            if not need_jd:
                item['description'] = j['description'][:cfg.get('jd_char_limit', 12000)]
                item['description_truncated'] = len(j['description']) > cfg.get('jd_char_limit', 12000)
            out.append(item)
        if q and taken.get(co, 0) < per_company:
            ring.append(q)
    return {'profile_sha256': psha, 'pending': len(cands), 'jobs': out}


def record(conn, profile, decisions):
    if not isinstance(decisions, list):
        raise ValueError('decisions must be a JSON list')
    psha, done = digest(profile), set()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for d in decisions:
            jid = s(d.get('job_id'), 'job_id', True)
            if jid in done:
                raise ValueError(f'duplicate decision {jid}')
            done.add(jid)
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if not row or d.get('job_sha256') != row['job_sha'] or d.get('profile_sha256') != psha:
                raise ValueError(f'stale or missing evidence for {jid}; read a fresh queue')
            j = json.loads(row['payload'])
            r = {k: slist(d.get(k), k) for k in ('fact_ids', 'gaps', 'followups', 'blockers', 'learning')}
            r['reason'] = s(d.get('reason'), 'reason', True)
            r['disposition'] = d.get('disposition')
            r['resume'] = s(d.get('resume', ''), 'resume')
            r['resume_note'] = s(d.get('resume_note', ''), 'resume_note')
            r['visa_risk'] = d.get('visa_risk', 'unknown')
            r['visa_note'] = s(d.get('visa_note', ''), 'visa_note')
            r['score'] = d.get('score', 0)
            if r['disposition'] not in DISPOSITIONS:
                raise ValueError(f'{jid}: disposition must be one of {DISPOSITIONS}')
            if r['resume'] not in RESUMES:
                raise ValueError(f'{jid}: resume must be one of {RESUMES}')
            if r['resume'] == 'custom' and not r['resume_note']:
                raise ValueError(f'{jid}: resume custom needs resume_note (base version + what to change, facts only)')
            if r['visa_risk'] not in VISA_RISK:
                raise ValueError(f'{jid}: visa_risk must be one of {VISA_RISK}')
            if not isinstance(r['score'], (int, float)) or not 0 <= r['score'] <= 10:
                raise ValueError(f'{jid}: score must be 0-10')
            bad = set(r['fact_ids']) - profile['facts'].keys()
            if bad:
                raise ValueError(f'{jid}: unknown fact ids {sorted(bad)}')
            for k in ('opening_checked', 'jd_complete'):
                r[k] = d.get(k, False)
                if type(r[k]) is not bool:
                    raise ValueError(f'{k} must be boolean')
            if r['disposition'] in RECOMMENDED:
                if not (j['description'] and j['apply_url'] and r['fact_ids'] and r['opening_checked']
                        and r['jd_complete'] and r['resume'] and not r['blockers']):
                    raise ValueError(f'{jid}: strong/stretch needs full JD, apply URL, facts, resume, checks and no blockers')
            conn.execute("""INSERT INTO reviews VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                job_sha=excluded.job_sha, profile_sha=excluded.profile_sha, reviewed_at=excluded.reviewed_at,
                payload=excluded.payload""", (jid, row['job_sha'], psha, now().isoformat(), dump(r)))
    return {'recorded': len(done)}


def set_description(conn, items, c):
    """items: [{job_id, description, apply_url?, status?}] gathered by WebFetch/browser/resolve."""
    n = 0
    with conn:
        for it in items:
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (it['job_id'],)).fetchone()
            if not row:
                continue
            j = json.loads(row['payload'])
            j['description'] = s(it.get('description'), 'description')
            if it.get('apply_url'):
                j['apply_url'] = it['apply_url']
            if it.get('status'):
                j['status'] = it['status']
            hits = screen(j['description'])
            state = row['state']
            if state in ('new', 'blocked'):
                state = 'blocked' if any(h['severity'] == 'block' for h in hits) else 'new'
            if j['status'] == 'closed':
                state = 'closed'
            conn.execute('UPDATE jobs SET payload=?, job_sha=?, screen=?, state=? WHERE id=?',
                         (dump(j), job_sha(j), dump(hits), state, it['job_id']))
            n += 1
    return {'updated': n}


# ---------------------------------------------------------------- shortlist
def md(t):
    return re.sub(r'([\\`*_\[\]<>|])', r'\\\1', ' '.join((t or '').split()))


RISK_ZH = {'low': '低', 'medium': '中', 'high': '高', 'unknown': '未知'}
RESUME_ZH = {'A': 'A 数据分析', 'B': 'B 软件/AI', 'C': 'C 机器人数据', 'custom': '需定制'}


def posting_key(j):
    """Same job posted under several URLs (e.g. one BambooHR id per city): same company, title and JD text.
    Returns None when the JD is too short to tell."""
    d = re.sub(r'\s+', ' ', j.get('description') or '').strip().lower()
    if len(d) < 200:
        return None
    norm = lambda x: re.sub(r'[^a-z0-9]+', ' ', (x or '').lower()).strip()
    return (norm(j.get('company')), norm(j.get('title')), sha256(d.encode()).hexdigest())


def closed_keys(conn):
    """posting keys of jobs the user already applied to or dropped: their twins must not come back."""
    keys = set()
    for r in conn.execute("SELECT payload FROM jobs WHERE state IN ('applied','dropped')"):
        k = posting_key(json.loads(r['payload']))
        if k:
            keys.add(k)
    return keys


def drop_twins(items, taken, key=lambda it: it[1]):
    """Keep the first item per posting key (items must be sorted best first); returns (kept, n_dropped)."""
    kept, n = [], 0
    for it in items:
        k = posting_key(key(it))
        if k and k in taken:
            n += 1
            continue
        if k:
            taken.add(k)
        kept.append(it)
    return kept, n


def shortlist(conn, profile, top, day, exclude_previous=True, minimum=0):
    psha = digest(profile)
    picked, held, stats = [], [], dict(strong=0, stretch=0, hold=0, skip=0, blocked=0, external_seen=0, pending=0)
    for row in conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state"):
        if row['state'] in stats:
            stats[row['state']] = row['n']
    prev = {r['id'] for r in conn.execute('SELECT id FROM shortlisted WHERE day<?', (day,))} if exclude_previous else set()
    for row in rows(conn):
        j = json.loads(row['payload'])
        if j['status'] == 'closed':
            continue
        if not is_current(row, psha):
            stats['pending'] += 1
            continue
        r = json.loads(row['review'])
        stats[r['disposition']] += 1
        if r['disposition'] in RECOMMENDED and row['id'] not in prev:
            (held if r['visa_risk'] == 'high' else picked).append((row['id'], j, r))
        elif r['disposition'] == 'hold':
            held.append((row['id'], j, r))
    picked.sort(key=lambda x: (x[2]['disposition'] != 'strong', -x[2]['score']))
    taken = closed_keys(conn)
    for r in conn.execute('SELECT j.payload FROM jobs j JOIN shortlisted s ON s.id=j.id WHERE s.day<?', (day,)):
        k = posting_key(json.loads(r['payload']))
        if k:
            taken.add(k)
    picked, stats['duplicate'] = drop_twins(picked, taken)
    held, _ = drop_twins(held, taken)
    top_items = picked[:top] if top else picked
    with conn:
        for jid, _, _ in top_items:
            conn.execute('INSERT OR IGNORE INTO shortlisted VALUES (?,?)', (jid, day))
    L = [f'# 每日候选清单 {day}', '',
         f"本期推荐 {len(top_items)} 个（{lane_mix(top_items)}）· 待核实/高签证风险 {len(held)} · "
         f"规则拦截 {stats['blocked']} · 与外部台账重复 {stats['external_seen']} · 待审 {stats['pending']}", '']
    if minimum and len(top_items) < minimum:
        L += [f'> ⚠ 合格岗位 {len(top_items)} 个，低于下限 {minimum} 个。', '']
    L += ['> 只是推荐，不代表已投递。投递前请再看一眼岗位是否开放，签证问题按申请表原文回答。', '',
          '| # | 公司 / 岗位 | 地点 | 简历 | 匹配度 | 签证风险 |', '|---|---|---|---|---|---|']
    for n, (jid, j, r) in enumerate(top_items, 1):
        L.append(f"| {n} | [{md(j['company'])} — {md(j['title'])}](<{j['apply_url'] or j['listing_url']}>) | "
                 f"{md(j['location'] or '未注明')} | {resume_cell(r)} | "
                 f"{r['disposition']} {r['score']}/10 | {RISK_ZH[r['visa_risk']]} |")
    for n, (jid, j, r) in enumerate(top_items, 1):
        L += ['', f"### {n}. {md(j['company'])} — {md(j['title'])}",
              f"- 链接：<{j['apply_url'] or j['listing_url']}>（来源：{', '.join(j.get('sources', []))}）",
              f"- 简历：{RESUME_ZH.get(r['resume'], r['resume'])}" + (f"；{md(r['resume_note'])}" if r['resume_note'] else ''),
              f"- 匹配理由：{md(r['reason'])}（依据：{', '.join(r['fact_ids'])}）"]
        if r['gaps']:
            L.append(f"- 缺口：{md('；'.join(r['gaps']))}")
        if r.get('learning'):
            L.append(f"- 在学标记：{md('、'.join(r['learning']))}（简历写 currently learning；收到面试再集中补）")
        L.append(f"- 签证风险：{RISK_ZH[r['visa_risk']]}" + (f"——{md(r['visa_note'])}" if r['visa_note'] else ''))
        if r['followups']:
            L.append(f"- 待问：{md('；'.join(r['followups']))}")
    if held:
        L += ['', '## 暂不推荐但保留（需你判断）', '']
        for jid, j, r in held[:15]:
            why = '；'.join(r['followups'] + ([r['visa_note']] if r['visa_note'] else []))
            L.append(f"- [{md(j['company'])} — {md(j['title'])}](<{j['apply_url'] or j['listing_url']}>) · "
                     f"{r['disposition']} · 签证{RISK_ZH[r['visa_risk']]} · {md(why)}")
    return '\n'.join(L) + '\n', {'listed': len(top_items), 'recommendable': len(picked), 'held': len(held),
                                  'below_minimum': bool(minimum and len(top_items) < minimum), **stats}


def lane_mix(items):
    n = {}
    for _, _, r in items:
        n[r['resume'] or '?'] = n.get(r['resume'] or '?', 0) + 1
    return ' / '.join(f"{RESUME_ZH.get(k, k)} {n[k]}" for k in ('B', 'A', 'C', 'custom', '?') if k in n) or '无'


def resume_cell(r):
    return RESUME_ZH.get(r['resume'], r['resume']) + (' · 在学' if r.get('learning') else '')


def learning_stats(labelled):
    """labelled: [(label, review)] -> [(skill, [labels])] sorted by how many open jobs carry that 'learning' flag."""
    c = {}
    for label, r in labelled:
        for k in dict.fromkeys(r.get('learning') or []):
            c.setdefault(k, []).append(label)
    return sorted(c.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def open_candidates(conn, day, max_age_days=14):
    """All recommended jobs ever listed that are still open: not applied/dropped/closed, review not older
    than max_age_days. One file for the application conversation instead of many daily lists."""
    first = {r['id']: r['d'] for r in conn.execute('SELECT id, MIN(day) d FROM shortlisted GROUP BY id')}
    cutoff = (datetime.fromisoformat(day) - timedelta(days=max_age_days)).date().isoformat()
    items, aged = [], 0
    for row in rows(conn):
        if row['id'] not in first or not row['review']:
            continue
        j, r = json.loads(row['payload']), json.loads(row['review'])
        if j['status'] == 'closed' or r['disposition'] not in RECOMMENDED:
            continue
        if row['reviewed_at'][:10] < cutoff:
            aged += 1
            continue
        items.append((row['id'], j, r, first[row['id']]))
    items.sort(key=lambda x: (x[3], x[2]['disposition'] != 'strong', -x[2]['score']))
    items, dup = drop_twins(items, closed_keys(conn))  # earliest-listed twin wins
    items.sort(key=lambda x: (x[2]['disposition'] != 'strong', -x[2]['score'], x[3]))
    L = [f'# 待处理候选岗位（截至 {day}）', '',
         f"共 {len(items)} 个（{lane_mix([(a, b, c) for a, b, c, _ in items])}）· 审读超过 {max_age_days} 天移出 {aged} 个", '',
         '> 每次跑完每日求职后自动重写。已投、OA、面试、拒、offer 的岗位在 `APPLICATIONS.md` 记成对应状态，'
         '放弃的记成“放弃”，下次运行就会从这里消失。完整理由见“首次列出”那天的每日清单。投递前请再确认岗位仍开放、截止日期未过。', '',
         ]
    stats = learning_stats([(j['company'], r) for _, j, r, _ in items])
    if stats:
        L += ['## 先补哪个技能（“在学”标记出现在几个待投岗位里）', '',
              '| 技能 | 岗位数 | 岗位 |', '|---|---|---|']
        L += [f"| {md(k)} | {len(v)} | {md('、'.join(v[:6]))}{' 等' if len(v) > 6 else ''} |" for k, v in stats]
        L += ['']
    L += ['| # | 首次列出 | 公司 / 岗位 | 简历 | 匹配度 | 签证风险 |', '|---|---|---|---|---|---|']
    for n, (jid, j, r, d) in enumerate(items, 1):
        L.append(f"| {n} | {d} | [{md(j['company'])} — {md(j['title'])}](<{j['apply_url'] or j['listing_url']}>) | "
                 f"{resume_cell(r)} | {r['disposition']} {r['score']}/10 | {RISK_ZH[r['visa_risk']]} |")
    for n, (jid, j, r, d) in enumerate(items, 1):
        L += ['', f"### {n}. {md(j['company'])} — {md(j['title'])}",
              f"- 链接：<{j['apply_url'] or j['listing_url']}>（首次列出 {d}，清单 `daily/{d}.md`）",
              f"- 地点：{md(j['location'] or '未注明')}",
              f"- 简历：{RESUME_ZH.get(r['resume'], r['resume'])}" + (f"；{md(r['resume_note'])}" if r['resume_note'] else '')]
        if r['gaps']:
            L.append(f"- 缺口：{md('；'.join(r['gaps']))}")
        if r.get('learning'):
            L.append(f"- 在学标记：{md('、'.join(r['learning']))}（简历写 currently learning）")
        if r['followups']:
            L.append(f"- 待问：{md('；'.join(r['followups']))}")
        if r['visa_note']:
            L.append(f"- 签证：{md(r['visa_note'])}")
    return '\n'.join(L) + '\n', {'open': len(items), 'aged_out': aged, 'duplicate': dup, 'learning': {k: len(v) for k, v in stats}}


def mark_dropped(conn, url):
    jid = find_existing(conn, {identity(url)})
    if not jid:
        raise ValueError('posting not found; use the saved listing/apply URL')
    with conn:
        conn.execute("UPDATE jobs SET state='dropped' WHERE id=?", (jid,))
    return {'dropped': jid}


def mark_applied(conn, url):
    jid = find_existing(conn, {identity(url)})
    if not jid:
        raise ValueError('posting not found; use the saved listing/apply URL')
    with conn:
        conn.execute("UPDATE jobs SET state='applied' WHERE id=?", (jid,))
    return {'applied': jid}


def status(conn):
    out = {r['state']: r['n'] for r in conn.execute('SELECT state, COUNT(*) n FROM jobs GROUP BY state')}
    out['reviews'] = conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
    out['no_jd'] = sum(1 for r in conn.execute("SELECT payload FROM jobs WHERE state='new'") if not json.loads(r[0])['description'])
    out['last_runs'] = [dict(r) for r in conn.execute('SELECT * FROM runs ORDER BY at DESC LIMIT 5')]
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workspace', type=Path, default=Path('.claude-job-search'))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('collect', help='fetch configured boards and/or Simplify, prefilter, store')
    p.add_argument('--only', choices=['boards', 'simplify', 'all'], default='all')
    p.add_argument('--board', help='single provider:slug (overrides config)')
    p.add_argument('--company')
    p = sub.add_parser('import', help='import manual/WebFetch/browser records (JSON list)')
    p.add_argument('--input', type=Path, required=True)
    p = sub.add_parser('external', help='mark jobs already delivered by another workflow (ledger JSON or URL list)')
    p.add_argument('--input', type=Path, action='append', required=True)
    p = sub.add_parser('resolve', help='fill missing JDs through public ATS APIs')
    p.add_argument('--limit', type=int, default=40)
    p = sub.add_parser('set-jd', help='store JD text gathered by WebFetch/browser')
    p.add_argument('--input', type=Path, required=True)
    p = sub.add_parser('queue')
    p.add_argument('--limit', type=int, default=15)
    p.add_argument('--per-company', type=int, default=2)
    p.add_argument('--lane', choices=['A', 'B', 'C'])
    p.add_argument('--need-jd', action='store_true', help='list leads that still need a JD fetch')
    p = sub.add_parser('record')
    p.add_argument('--input', type=Path, required=True)
    p = sub.add_parser('shortlist')
    p.add_argument('--top', type=int, default=0, help='0 = list every recommendable job (no upper cap)')
    p.add_argument('--day', default=None)
    p.add_argument('--out', type=Path)
    p = sub.add_parser('mark-applied')
    p.add_argument('--url', required=True)
    p = sub.add_parser('mark-dropped', help='user decided not to apply')
    p.add_argument('--url', required=True)
    p = sub.add_parser('open', help='write the cumulative list of still-open recommended jobs')
    p.add_argument('--day', default=None)
    p.add_argument('--max-age-days', type=int, default=14)
    p.add_argument('--out', type=Path)
    sub.add_parser('status')
    sub.add_parser('refilter', help='re-apply prefilter after editing config.json')
    sub.add_parser('rescreen', help='re-run JD screening (incl. hard blocks) on unreviewed jobs after a rule change')
    a = ap.parse_args(argv)
    try:
        ws = a.workspace
        cfg = load(ws / 'config.json')
        c = compile_cfg(cfg)
        HARD.update({k: v for k, v in cfg.get('hard_requirements', {}).items() if not k.startswith('_')})
        with closing(open_db(ws)) as conn:
            if a.cmd == 'collect':
                result = {'sources': {}, 'errors': {}}
                boards = [{'board': a.board, 'company': a.company or a.board}] if a.board else (
                    cfg['boards'] if a.only in ('boards', 'all') else [])
                for b in boards:
                    prov, _, slug = b['board'].partition(':')
                    try:
                        recs, seen = fetch_board(prov, slug, b['company'], c)
                        result['sources'][b['board']] = {'seen': seen, 'kept': len(recs)} | store(conn, recs, c, b['board'])
                    except Exception as e:  # keep going; last saved data stays
                        result['errors'][b['board']] = f'{type(e).__name__}: {e}'[:300]
                if a.only in ('simplify', 'all') and not a.board and cfg.get('simplify', {}).get('enabled'):
                    try:
                        recs, seen = fetch_simplify(cfg, c)
                        result['sources']['simplify'] = {'seen_active': seen, 'kept': len(recs)} | store(conn, recs, c)
                    except Exception as e:
                        result['errors']['simplify'] = f'{type(e).__name__}: {e}'[:300]
                result['external_rematched'] = mark_external(conn)
                result['refiltered_out'] = refilter(conn, c)
            elif a.cmd == 'import':
                data = load(a.input)
                recs = [rec(x['company'], x['title'], x['listing_url'], x.get('description', ''), x.get('location', ''),
                            x.get('apply_url', ''), x.get('posted', ''), x.get('source', 'manual'), x.get('status', 'listed'))
                        for x in data]
                result = store(conn, recs, c) | {'external_rematched': mark_external(conn)}
            elif a.cmd == 'external':
                result = {}
                for f in a.input:
                    result[str(f)] = import_external(conn, f)
            elif a.cmd == 'resolve':
                allq = queue(conn, {'facts': {'x': 'x'}}, cfg, 100000, 100000, need_jd=True)['jobs']
                todo = [t for t in allq if api_resolvable(t['apply_url'] or t['listing_url'])][:a.limit]
                result_skipped = len(allq) - len(todo)
                items, fails = [], {}
                pg, pt = polite(get_json), polite(get_text)
                for t in todo:
                    try:
                        d = resolve_jd(t)
                        got = {'description': d} if d else resolve_ext(t, get=pg, get_html=pt)
                        if got.get('description') or got.get('status'):
                            items.append({'job_id': t['job_id'], **got})
                        else:
                            fails[t['job_id']] = 'no public API; use WebFetch'
                    except HTTPError as e:
                        if e.code == 410:  # explicit "Gone" from the posting page
                            items.append({'job_id': t['job_id'], 'description': '', 'status': 'closed'})
                        else:
                            fails[t['job_id']] = f'HTTPError {e.code}'
                    except Exception as e:
                        fails[t['job_id']] = f'{type(e).__name__}: {e}'[:200]
                result = set_description(conn, items, c) | {'tried': len(todo), 'not_api_resolvable': result_skipped,
                                                            'needs_webfetch_or_failed': fails}
            elif a.cmd == 'set-jd':
                result = set_description(conn, load(a.input), c)
            elif a.cmd == 'refilter':
                result = {'refiltered_out': refilter(conn, c)}
            elif a.cmd == 'status':
                result = status(conn)
            elif a.cmd == 'mark-applied':
                result = mark_applied(conn, a.url)
            elif a.cmd == 'mark-dropped':
                result = mark_dropped(conn, a.url)
            elif a.cmd == 'open':
                text, result = open_candidates(conn, a.day or datetime.now().strftime('%Y-%m-%d'), a.max_age_days)
                if a.out:
                    a.out.parent.mkdir(parents=True, exist_ok=True)
                    a.out.write_text(text, encoding='utf-8')
                else:
                    print(text)
            else:
                profile = profile_at(ws)
                if a.cmd == 'queue':
                    result = queue(conn, profile, cfg, a.limit, a.per_company, a.lane, a.need_jd)
                elif a.cmd == 'record':
                    result = record(conn, profile, load(a.input))
                elif a.cmd == 'rescreen':
                    result = rescreen(conn, profile)
                else:
                    day = a.day or datetime.now().strftime('%Y-%m-%d')
                    text, result = shortlist(conn, profile, a.top, day,
                                             minimum=cfg.get('budgets', {}).get('shortlist_min', 0))
                    if a.out:
                        a.out.parent.mkdir(parents=True, exist_ok=True)
                        a.out.write_text(text, encoding='utf-8')
                    else:
                        print(text)
            if a.cmd not in ('status', 'queue'):
                log_run(conn, a.cmd, {k: v for k, v in result.items() if k != 'needs_webfetch_or_failed'} if isinstance(result, dict) else result)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f'cjs: {type(e).__name__}: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
