"""Read-only address lookup using the state indexes installed with offline maps.

Outside the US an index is named for its region (CA-ON.sqlite, packaging/regions) and its `state` column holds that code.
Canadian province names, their Canada Post abbreviations after a comma or before a postal code, and postal codes
written A1A 1A1 pick a Canadian index. When no index for that province is installed, the words are read as before.
"""
import math
import re
import sqlite3
from pathlib import Path

from engine.paths import ro_uri

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
# Canada's provinces and territories, with Canada Post's abbreviations. None of these abbreviations is a US code.
PROVINCES = {'alberta': 'AB', 'british columbia': 'BC', 'manitoba': 'MB', 'new brunswick': 'NB', 'newfoundland and labrador': 'NL',
             'nova scotia': 'NS', 'northwest territories': 'NT', 'nunavut': 'NU', 'ontario': 'ON', 'prince edward island': 'PE',
             'quebec': 'QC', 'québec': 'QC', 'saskatchewan': 'SK', 'yukon': 'YT'}
PROVINCE_CODES = set(PROVINCES.values())
POSTCODE_FIRST = {'A': 'NL', 'B': 'NS', 'C': 'PE', 'E': 'NB', 'G': 'QC', 'H': 'QC', 'J': 'QC', 'K': 'ON', 'L': 'ON', 'M': 'ON',
                  'N': 'ON', 'P': 'ON', 'R': 'MB', 'S': 'SK', 'T': 'AB', 'V': 'BC', 'Y': 'YT'}     # Canada Post; X is two territories
CA_POSTCODE = re.compile(r'[\s,]*\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])\s?(\d[ABCEGHJ-NPRSTV-Z]\d)\s*$', re.I)
DIRECTIONS = {'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'}
STREET_TYPES = {'blvd', 'ave', 'st', 'dr', 'rd', 'pl', 'ct', 'ln', 'ter', 'cir', 'hwy', 'pkwy', 'sq'}
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


def street_forms(street, canadian=False):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    suffix_direction = canadian and len(words) >= 3 and words[-2] in STREET_TYPES and words[-1] in DIRECTIONS
    if canadian and len(words) >= 3 and words[0] in DIRECTIONS and words[-1] in STREET_TYPES:
        forms += [' '.join([*parts[1:], parts[0]]) for parts in map(str.split, forms[:])]
    elif suffix_direction:
        forms += [' '.join([parts[-1], *parts[:-1]]) for parts in map(str.split, forms[:])]
    if words[0] not in DIRECTIONS and not suffix_direction:
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _canada(remainder, installed=()):
    """A Canadian postal code and province at the end of the text: (the rest, region code or '', postcode or '')."""
    postcode = province = ''
    match = CA_POSTCODE.search(remainder)
    if match:
        postcode = f'{match[1]} {match[2]}'.upper()
        remainder = remainder[:match.start()]
    parts = [part.strip() for part in remainder.split(',')]
    words = remainder.replace(',', ' ').split()
    if len(parts) > 1 and parts[-1].upper() in PROVINCE_CODES:                  # "Toronto, ON"
        province = parts[-1].upper()
        remainder = ','.join(parts[:-1])
    elif len(words) > 1 and words[-1].upper() in PROVINCE_CODES and (postcode or f'CA-{words[-1].upper()}' in installed):   # "Toronto ON M5H 2N2"
        province = words[-1].upper()
        remainder = remainder[:remainder.rstrip().rfind(words[-1])]
    else:
        lowered = [word.lower() for word in words]
        for count in (4, 3, 2, 1):
            if len(lowered) > count and ' '.join(lowered[-count:]) in PROVINCES:   # "Victoria, British Columbia"
                province = PROVINCES[' '.join(lowered[-count:])]
                remainder = ' '.join(words[:-count])
                break
    if postcode and not province:
        province = POSTCODE_FIRST.get(postcode[0], '')
    return remainder, (f'CA-{province}' if province else ''), postcode


def _parts(query, canada=True, installed=()):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    state = postcode = ''
    if canada:
        remainder, state, postcode = _canada(remainder, installed)
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    if not postcode and remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    for count in (3, 2, 1):
        if state or len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house."""
    if not is_address_query(query):
        return []
    geo_directory = Path(geo_directory)
    paths = sorted((geo_directory / 'addresses').glob('*.sqlite'))
    installed = {path.stem.upper() for path in paths}
    number, words, state, postcode = _parts(query, installed=installed)
    if state.startswith('CA-') and state not in installed:
        number, words, state, postcode = _parts(query, canada=False)    # no index for that province: read the words as before
    if not words:
        return []
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append((sqlite3.connect(ro_uri(path), uri=True), path.stem.upper().startswith('CA-')))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            forms = {canadian: street_forms(street, canadian) for canadian in (False, True)}
            for connection, canadian in connections:
                for form in forms[canadian]:
                    conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                    parameters = [number, number.lower(), form, form + '\uffff']
                    if state:
                        conditions.append('state = ?')
                        parameters.append(state)
                    if postcode:
                        conditions.append('zip = ?')
                        parameters.append(postcode)
                    if city:
                        conditions.append('lower(city) LIKE ?')
                        parameters.append(city + '%')
                    sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                           + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT ?')
                    try:
                        rows.extend(connection.execute(sql, [*parameters, limit * 3]).fetchall())
                    except sqlite3.Error:
                        continue
            if rows:
                def rank(row):
                    distance = 0
                    if near:
                        distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
                    return len(row[7]), distance, row[3], row[2], row[1]
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=rank):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, (state_code or '').removeprefix('CA-'), zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection, _ in connections:
            connection.close()
