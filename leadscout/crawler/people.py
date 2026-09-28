"""People and their designations (job titles) on a business's own website.

Small businesses usually name their people on team / about / contact pages:
a card with a name and a title, a line like "Mark Chen - Office Manager", or
structured data for search engines (schema.org Person). We read those, then
link each email to its person, e.g. anna@smilestudio.com -> Anna Lopez, Owner.

Shared inboxes get a designation from the address itself (sales@ -> Sales).
Nothing is guessed: when the website doesn't say, the designation stays empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

TITLE_WORDS = (
    r"co-?owner|owner|proprietor|co-?founder|founder|chief executive officer|chief [a-z]+ officer|ceo|coo|cfo|cto|"
    r"vice president|president|principal|managing partner|senior partner|partner|managing member|"
    r"managing director|director|general manager|office manager|practice manager|operations manager|"
    r"project manager|sales manager|manager|head of [a-z&]+|team lead|lead [a-z]+|supervisor|foreman|"
    r"superintendent|estimator|administrator|coordinator|receptionist|hygienist|dental assistant|assistant|"
    r"associate|attorney|lawyer|paralegal|accountant|bookkeeper|broker|realtor|agent|consultant|advisor|"
    r"adviser|technician|engineer|architect|designer|chef|dentist|orthodontist|veterinarian|physician|"
    r"doctor|therapist|chiropractor|optometrist|pharmacist|nurse practitioner|dds|dmd|dvm|cpa|esq"
)
TITLE_RE = re.compile(rf"\b(?:{TITLE_WORDS})\b", re.I)
# Seniority, most senior first. The first level whose words appear in a title wins.
SENIORITY = [
    (1, re.compile(r"\b(?:co-?owner|owner|proprietor|co-?founder|founder|chief executive|ceo|principal|"
                   r"managing partner|senior partner|managing member|managing director|executive director|"
                   r"general partner|chair(?:man|woman|person)?)\b|(?<!vice )(?<!vice-)\bpresident\b", re.I)),
    (2, re.compile(r"\b(?:chief [a-z]+ officer|coo|cfo|cto|cmo|cio|vice[\s-]*president|vp|director|"
                   r"general manager|head of|partner)\b", re.I)),
    (3, re.compile(r"\b(?:manager|mgr|supervisor|superintendent|administrator|team lead|foreman)\b", re.I)),
]
SENIORITY_NAMES = {1: "Owner / CEO", 2: "Executive / Director", 3: "Manager", 4: "Staff"}
# Words that make a title junior whatever else it says ("Assistant Director", "Associate Partner").
JUNIOR_RE = re.compile(r"\b(?:assistant|associate|asst|intern|trainee|partner program)\b", re.I)


def seniority(title: str | None) -> int | None:
    """1 owner / CEO / president, 2 executive / director / VP, 3 manager, 4 other staff;
    None without a title."""
    if not title:
        return None
    for level, regex in SENIORITY:
        if regex.search(title):
            # "Irrigation Dept. Mgr & Owner" is an owner; "Assistant Director" is staff
            return 4 if JUNIOR_RE.search(title) and level > 1 else level
    return 4

_NAME_WORD = r"[A-Z][a-z'’\-]+(?:-[A-Z][a-z'’]+)?"
NAME_RE = re.compile(rf"^(?:(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+)?({_NAME_WORD}(?:\s+[A-Z]\.)?(?:\s+{_NAME_WORD}){{1,2}})$")
_SEP = r"\s*(?:,|–|—|-|\||:|/)\s*"
NAME_THEN_TITLE = re.compile(rf"^(?:(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+)?(?P<name>{_NAME_WORD}(?:\s+[A-Z]\.)?(?:\s+{_NAME_WORD}){{1,2}})"
                             rf"{_SEP}(?P<title>.{{2,60}})$")
TITLE_THEN_NAME = re.compile(rf"^(?P<title>.{{2,40}}?){_SEP}(?:(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+)?"
                             rf"(?P<name>{_NAME_WORD}(?:\s+[A-Z]\.)?(?:\s+{_NAME_WORD}){{1,2}})$")
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Staff are often listed by first name only: "Devin - Office Manager".
FIRST_NAME_THEN_TITLE = re.compile(r"^(?P<name>[A-Z][a-z]{2,15})(?:\s*[–—|]\s*|\s+-\s+)(?P<title>.{2,50})$")
# "Dr. Craig Larson" anywhere in the text (bios, intros, reviews).
DOCTOR_RE = re.compile(rf"\bDr\.?\s+({_NAME_WORD}(?:\s+[A-Z]\.)?\s+{_NAME_WORD})")
CREDENTIALS_RE = re.compile(r"\b(?:dds|dmd|md|do|dvm|od|dc|cpa|esq|phd|jd|pc|pllc|llc|inc|rn|np|pa)\b\.?", re.I)
# Professional credentials of a person (not company suffixes like LLC / Inc): "Dr. Ken Mickens DDS".
PERSON_CREDENTIALS_RE = re.compile(r"\b(?:dds|dmd|md|dvm|od|dc|cpa|esq|phd|jd|rn|np)\b\.?", re.I)

# Words that look like names on web pages but are buttons, headings or places.
NOT_NAMES = set("""
contact us our team meet the about read more learn book now call today home services service
get in touch privacy policy terms of use all rights reserved follow us sign up log in email phone
office hours location locations directions menu new patients patient forms reviews testimonials
blog news careers gallery free estimate request quote our story mission values welcome to why
choose schedule appointment appointments online payment insurance financing faq faqs hours open
monday tuesday wednesday thursday friday saturday sunday street avenue road suite drive lane
north south east west united states texas california florida hawaii colorado new york view profile
""".split())
# Words in business names, never in a person's name ("Boulder Premier Dentistry, Dentist in ...").
BUSINESS_WORDS = set("""
dental dentistry dentists orthodontics endodontics periodontics smile smiles family clinic clinics center
centre care health medical group associates partners company co corp inc llc pllc ltd services solutions
studio studios arts design designs law legal firm office offices practice construction contractors
builders roofing plumbing electric electrical heating cooling landscaping cleaning realty properties
agency consulting accounting tax insurance financial capital premier canyon valley mountain city county
""".split())

# Designations of shared inboxes, from the address itself.
INBOX_DESIGNATIONS = {
    "General inbox": "info contact contactus hello hi mail email enquiries enquiry inquiries inquiry general "
                     "welcome team dental clinic practice studio law",
    "Front desk / Office": "office admin administration reception frontdesk front.desk frontoffice",
    "Sales": "sales",
    "Billing / Accounts": "billing accounts accounting invoices invoice finance financial financials payments ar ap",
    "Customer support": "support help service services customerservice care",
    "HR / Hiring": "hr careers jobs recruiting recruitment hiring",
    "Marketing": "marketing",
    "Social media": "facebook fb instagram insta ig twitter tiktok youtube linkedin pinterest social socials "
                    "socialmedia social.media",
    "Press / Media": "press media pr",
    "Bookings": "appointments appointment booking bookings reservations schedule scheduling",
    "Patient inbox": "patient patients newpatient newpatients",
    "Orders": "orders order",
    "Office manager": "manager officemanager office.manager",
    "Legal": "legal privacy",
    "Website": "webmaster",
    "Operations": "operations ops",
    "Purchasing": "purchasing procurement",
    "Events": "events",
    "Feedback": "feedback",
}
_INBOX = {local: label for label, words in INBOX_DESIGNATIONS.items() for local in words.split()}


@dataclass
class Person:
    name: str
    title: str | None = None
    email: str | None = None
    url: str | None = None

    def __post_init__(self):
        self.name = _plain_name(self.name)
        if self.name.isupper():                          # "PHILIP TURTURRO" -> "Philip Turturro"
            self.name = self.name.title()
        if self.title and self.title.isupper() and len(self.title) > 4:
            self.title = CREDENTIALS_RE.sub(lambda m: m.group(0).upper(), self.title.title())

    @property
    def decision_maker(self) -> bool:
        """Owner, CEO, president, director, VP, chief officer, general manager, partner."""
        return (seniority(self.title) or 9) <= 2

    @property
    def level(self) -> int:
        return seniority(self.title) or 5


# Distinctive inbox words also count inside longer addresses (drdanielfrontdesk@, smithbilling@).
_INBOX_PARTS = [(w.replace(".", ""), label) for label, words in INBOX_DESIGNATIONS.items()
                if label != "General inbox" for w in words.split() if len(w) >= 6 and w not in
                ("orders", "events", "social", "socials", "privacy", "service", "services", "schedule")]


def inbox_designation(email: str) -> str | None:
    local = email.split("@", 1)[0].lower().split("+")[0]
    if local in _INBOX:
        return _INBOX[local]
    letters = re.sub(r"[^a-z]", "", local)
    for word, label in _INBOX_PARTS:
        if word in letters:
            return label
    return None


def is_decision_maker(title: str | None) -> bool:
    return Person("x", title).decision_maker


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" \t\r\n,;·•")


def _plain_name(name: str) -> str:
    """'Dr. Daniel Alleman, DDS' -> 'Daniel Alleman'."""
    name = re.sub(r"^(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+", "", _clean(name))
    name = re.sub(rf"(?:,?\s*{CREDENTIALS_RE.pattern})+$", "", name, flags=re.I)
    return _clean(name)


def _uncap(line: str) -> str:
    """ALL-CAPS lines ("JEFFREY PATRICIAN, DDS") are read in title case."""
    if len(line) <= 60 and line.isupper() and sum(c.isalpha() for c in line) >= 5:
        line = CREDENTIALS_RE.sub(lambda m: m.group(0).upper(), line.title())
    return line


def _valid_name(name: str) -> bool:
    words = [w.strip(".").lower() for w in name.split()]
    if not 2 <= len([w for w in words if len(w) > 1]) <= 3:
        return False
    if any(w in NOT_NAMES or w in BUSINESS_WORDS for w in words):
        return False
    return not TITLE_RE.search(name)


def _valid_title(title: str) -> bool:
    title = _clean(title)
    return bool(title) and len(title.split()) <= 8 and bool(TITLE_RE.search(title)) and "@" not in title


def _person_from_line(line: str) -> Person | None:
    person = _person_from_segment(line)
    if person is None:
        # "Questions about your bill? Tom Wu - Billing Coordinator": the part after a sentence.
        parts = re.split(r"(?<=[.?!])\s+", line)
        if len(parts) > 1:
            person = _person_from_segment(parts[-1])
    return person


def _person_from_segment(line: str) -> Person | None:
    m = FIRST_NAME_THEN_TITLE.match(line)
    if m and m.group("name").lower() not in NOT_NAMES and not TITLE_RE.search(m.group("name")) \
            and _valid_title(m.group("title")):
        return Person(m.group("name"), _clean(m.group("title")))
    m = NAME_THEN_TITLE.match(line)
    if m and _valid_name(m.group("name")) and _valid_title(m.group("title")):
        return Person(_clean(m.group("name")), _clean(m.group("title")))
    m = TITLE_THEN_NAME.match(line)
    if m and _valid_name(m.group("name")) and _valid_title(m.group("title")):
        return Person(_clean(m.group("name")), _clean(m.group("title")))
    return None


def _name_only(line: str) -> str | None:
    m = NAME_RE.match(line)
    return _clean(m.group(1)) if m and _valid_name(m.group(1)) else None


def people_from_jsonld(raw_html: str) -> list[Person]:
    out = []
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", raw_html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Person" in types and isinstance(node.get("name"), str) and _valid_name(node["name"]):
                email = str(node.get("email") or "").replace("mailto:", "").strip().lower() or None
                title = node.get("jobTitle")
                out.append(Person(_clean(node["name"]), _clean(title) if isinstance(title, str) else None, email))
            for key in ("founder", "employee", "employees", "member", "members", "@graph", "author"):
                if key in node:
                    child = node[key]
                    if isinstance(child, dict) and "@type" not in child:
                        child = {**child, "@type": "Person"}
                    stack.append(child)
    return out


def _could_be(email: str, person: Person) -> bool:
    """Can this address belong to this person? Yes when named like them, or when it's a
    shared inbox (info@, office@); no when it looks like another person's name."""
    if inbox_designation(email) or match_person(email, [person]):
        return True
    letters = re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())
    return len(letters) < 4          # initials (jd@, j.d2@) are too short to judge


def people_from_lines(lines: list[str]) -> list[Person]:
    """'Name - Title' on one line, or a name line followed by a title line (team cards).
    An email up to three lines below a person (or right above one) is linked to them."""
    people: list[tuple[int, Person]] = []
    for i, line in enumerate(lines):
        if len(line) > 120:
            continue
        person = _person_from_line(line)
        if person is None:
            name = _name_only(line)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if name and len(nxt) <= 70 and _valid_title(nxt) and _name_only(nxt) is None:
                person = Person(name, _clean(nxt))
        if person:
            people.append((i, person))
    for i, line in enumerate(lines):
        for email in EMAIL_IN_TEXT.findall(line):
            free = [(j, p) for j, p in people if p.email is None]
            # On cards the address follows its person: prefer the closest person above,
            # and only take one below if it is on the very next line.
            # An address named after someone else (juancantu@ next to Joe Guevara) is not theirs.
            free = [(j, p) for j, p in free if _could_be(email, p)]
            above = [(j, p) for j, p in free if i - 3 <= j <= i]
            below = [(j, p) for j, p in free if j == i + 1]
            if above:
                max(above, key=lambda t: t[0])[1].email = email.lower()
            elif below:
                below[0][1].email = email.lower()
    return [p for _, p in people]


_FULL_NAME = rf"(?:Dr\.?\s+)?({_NAME_WORD}(?:\s+[A-Z]\.)?\s+{_NAME_WORD})"
_ROLE = (r"(co-?owner|owner|co-?founder|founder|president|ceo|chief executive officer|managing partner|"
         r"general manager|executive director|director|principal|proprietor)")
# Owners and leaders named in sentences on about / story pages.
PROSE_PATTERNS = [
    (re.compile(rf"\b(?:founded|started|established)\s+(?:in\s+\d{{4}}\s+)?by\s+{_FULL_NAME}"), "Founder"),
    (re.compile(rf"\bowned\s+(?:and\s+operated\s+)?by\s+{_FULL_NAME}"), "Owner"),
    (re.compile(rf"\b[Oo]ur\s+{_ROLE}[,:]?\s+{_FULL_NAME}", re.I), None),
    (re.compile(rf"{_FULL_NAME},?\s+(?:is\s+)?(?:the\s+|our\s+)?{_ROLE}\s+(?:of|and|at)\b", re.I), None),
    (re.compile(rf"{_FULL_NAME}\s+(?:founded|started)\s+(?:the|this|our)\s+(?:company|business|firm|practice)"),
     "Founder"),
]
_ROLE_CASE = {"ceo": "CEO", "co-owner": "Co-Owner", "coowner": "Co-Owner", "co-founder": "Co-Founder",
              "cofounder": "Co-Founder", "chief executive officer": "CEO"}


def people_from_prose(text: str) -> list[Person]:
    """"Founded in 1998 by John Smith", "owned and operated by Maria Lopez", "our president,
    Tom Wu", "Anna Reyes, owner of ..." -> people with the role the sentence gives them."""
    out = []
    for regex, fixed in PROSE_PATTERNS:
        for m in regex.finditer(text):
            groups = [g for g in m.groups() if g]
            name = next((g for g in groups if g[:1].isupper() and " " in g), None)
            role = fixed or next((g for g in groups if g is not name), None)
            if not name or not role or not _valid_name(name):
                continue
            role = _ROLE_CASE.get(role.lower(), role.title() if role.islower() else role)
            out.append(Person(_clean(name), role))
    return out


def find_people(raw_html: str, soup: BeautifulSoup, url: str | None = None) -> list[Person]:
    """People named on a page (``soup`` should already have scripts removed)."""
    for a in soup.select('a[href^="mailto:"]'):          # keep the address next to its name
        address = a["href"][7:].split("?")[0]
        if address and address not in a.get_text():
            a.append(f" {address}")
    lines = [_clean(l) for l in soup.get_text("\n").split("\n")]
    lines = [_uncap(l) for l in lines if l]
    found = people_from_jsonld(raw_html) + people_from_lines(lines)
    for line in lines:                                  # doctors named anywhere in the text
        for name in DOCTOR_RE.findall(line):
            if _valid_name(name):
                found.append(Person(_clean(name), "Doctor"))
    found += people_from_prose(" ".join(lines))
    merged: dict[str, Person] = {}
    for p in found:
        p.url = url
        key = p.name.lower()
        if key in merged:
            old = merged[key]
            # keep the most senior title said about them ("Dentist" on a card, "founder" in the story)
            if not old.title or old.title == "Doctor" or (p.title and p.level < old.level):
                old.title = p.title or old.title
            old.email = old.email or p.email
        else:
            merged[key] = p
    return list(merged.values())


def _name_parts(name: str) -> tuple[str, str]:
    words = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split() if not w.endswith(".") or len(w) > 2]
    words = [w for w in words if w and w not in ("dr", "mr", "mrs", "ms")]
    if len(words) == 1:
        return words[0], ""
    return (words[0], words[-1]) if len(words) >= 2 else ("", "")


def _local_forms(local: str) -> set[str]:
    """The address's letters, also without a leading 'dr' or trailing credentials (craiglarsondds)."""
    letters = re.sub(r"[^a-z]", "", local)
    forms = {letters}
    stripped = re.sub(r"^dr", "", letters)
    for _ in range(2):
        stripped = re.sub(r"(dds|dmd|md|dvm|cpa|esq|phd|law|dental|ortho|office)$", "", stripped)
    if len(stripped) >= 3:
        forms.add(stripped)
    return forms


def match_person(email: str, people: list[Person]) -> Person | None:
    """The person an address belongs to: listed with it, or named like it
    (anna@, alopez@, anna.lopez@, drannalopez@, craiglarsondds@)."""
    email = email.lower()
    for p in people:
        if p.email and p.email.lower() == email:
            return p
    local = email.split("@", 1)[0]
    tokens = [t for t in re.split(r"[._\-+]", local) if t]
    letters = _local_forms(local)
    hits = []
    for p in people:
        first, last = _name_parts(p.name)
        if not first:
            continue
        if last:
            forms = {first, last, first + last, last + first, first[0] + last, first + last[0], last + first[0]}
            token_hit = tokens and tokens[0] == first and (len(tokens) == 1 or tokens[1] in (last, last[0]))
        else:
            forms, token_hit = {first}, tokens and tokens[0] == first and len(tokens) == 1
        # the surname inside the address: billhrncir@ (Bill = Billy Hrncir), drsmith@
        surname_in = len(last) >= 5 and any(last in form for form in letters)
        if letters & forms or token_hit or surname_in:
            hits.append(p)
    return hits[0] if len(hits) == 1 else None


def not_the_business(people: list[Person], business_name: str | None) -> list[Person]:
    """Drop "people" that are really the business ("Mario's Welding (Owner)" at Mario's Welding LLC)."""
    biz = set(re.sub(r"[^a-z ]", " ", (business_name or "").lower().replace("'s", "")).split())
    out = []
    for p in people:
        words = set(re.sub(r"[^a-z ]", " ", p.name.lower().replace("'s", "")).split())
        if words and words <= biz:
            continue
        out.append(p)
    return out


def people_list(people: list[Person], owner: Person | None = None, limit: int = 12) -> list[dict]:
    """The company's people for storing / exporting, most senior first."""
    def level(p):
        return 1 if p is owner else p.level
    out = []
    for p in sorted(people, key=level)[:limit]:
        out.append({"name": p.name, "title": p.title or "", "email": p.email or "", "level": level(p)})
    return out


def owner_of(people: list[Person], business_name: str | None, domain: str | None) -> tuple[Person, str] | None:
    """The business's owner / decision maker, and why we think so.

    1. someone the website calls owner, founder, CEO, president, managing partner ...
    2. otherwise someone whose surname the business is named after
       ("Larson Dental" -> Dr. Craig Larson; "Weissman Family Dental" -> John Weissman)
    3. otherwise the business name itself is a person with a credential ("Dr. Ken Mickens DDS"),
       or a person whose surname is in the website address ("Kirk Rathburn", rathburndental.com)
    """
    deciders = sorted((p for p in people if p.decision_maker), key=lambda p: p.level)
    if deciders:
        return deciders[0], "title on website"
    name_words = set(re.sub(r"[^a-z ]", " ", (business_name or "").lower()).split())
    domain_letters = re.sub(r"[^a-z]", "", (domain or "").split(".")[0].lower())
    named = []
    for p in people:
        first, last = _name_parts(p.name)
        if last and len(last) >= 4 and (last in name_words or last in domain_letters):
            named.append(p)
    if len({p.name for p in named}) == 1:
        return named[0], "business named after them"
    raw = business_name or ""
    if re.search(r"^\s*Dr\.?\s", raw) or PERSON_CREDENTIALS_RE.search(raw):
        candidate = _clean(re.split(r"\s+[-–|]\s+", raw)[0])
        candidate = re.sub(r"^Dr\.?\s+", "", candidate)
        candidate = _clean(CREDENTIALS_RE.sub("", candidate).replace(",", " "))
        if NAME_RE.match(candidate) and _valid_name(candidate):
            return Person(candidate, "Owner"), "business named after them"
    # "Kirk Rathburn" at rathburndental.com: exactly First Last, the surname in the domain, the
    # first name not, and the rest of the domain a trade word. "Eagle Cold Storage" is no person.
    candidate = _clean(raw)
    words = candidate.split()
    if len(words) == 2 and NAME_RE.match(candidate) and _valid_name(candidate):
        first, last = _name_parts(candidate)
        rest = domain_letters.replace(last, "", 1)
        if len(last) >= 4 and last in domain_letters and first not in domain_letters \
                and first not in _TRADE_WORDS and last not in _TRADE_WORDS \
                and (rest == "" or rest in _TRADE_WORDS):
            return Person(candidate, "Owner"), "business named after them"
    return None


# Trade words: in business names and domains, never a person's name.
_TRADE_WORDS = BUSINESS_WORDS | set("""
storage rentals rental farms farm industries systems walls brokerage chill tours buses bus transport trucking
logistics warehouse cold landscapes landscape lawn lawns sprinkler sprinklers irrigation nursery garden gardens
tree trees pest fence fencing glass pools pool hvac air plumber plumbers electricians painting painters
flooring floors cabinets cabinetry kitchen kitchens bath baths remodeling roofers movers moving mini self
auto autos cars motors repair repairs shop store stores supply supplies hotel hotels inn motel suites
dentist dds dmd law lawyers attorneys cpa cpas accounting tax realty homes home builders contractor contractors
""".split())
