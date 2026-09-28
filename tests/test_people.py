"""Names and designations of the people behind the emails."""

import pytest
from bs4 import BeautifulSoup

from conftest import fixture_html
from leadscout.crawler.extract import extract_page
from leadscout.crawler.people import (
    Person,
    find_people,
    inbox_designation,
    is_decision_maker,
    match_person,
    people_from_lines,
)


def people_on(html):
    return {p.name: p for p in find_people(html, BeautifulSoup(html, "lxml"), "https://x.com/about")}


def test_team_page_layouts():
    people = people_on(fixture_html("about.html"))
    assert set(people) == {"Anna Lopez", "Mark Chen", "Sara Diaz", "Tom Wu"}
    assert people["Anna Lopez"].title == "Owner & Lead Dentist"          # structured data + card
    assert people["Anna Lopez"].decision_maker
    assert people["Mark Chen"].title == "Office Manager"
    assert people["Mark Chen"].email == "mark@smilestudio.com"           # mailto inside the card
    assert not people["Mark Chen"].decision_maker
    assert people["Sara Diaz"].title == "Dental Hygienist"
    assert people["Tom Wu"].title == "Billing Coordinator"               # "Name - Title" in a sentence line


def test_buttons_and_headings_are_not_people():
    people = people_on("<p>Contact Us</p><p>Owner</p><h2>Our Team</h2><p>Office Manager</p>"
                       "<p>Read More</p><p>Book Now - Owner</p><p>Main Street</p><p>Manager</p>")
    assert people == {}


@pytest.mark.parametrize("line,name,title", [
    ("Mark Chen - Office Manager", "Mark Chen", "Office Manager"),
    ("Mark Chen, Owner", "Mark Chen", "Owner"),
    ("Owner: Mark Chen", "Mark Chen", "Owner"),
    ("Dr. Anna Lopez | Founder", "Anna Lopez", "Founder"),
    ("Jean-Paul Martin – Managing Partner", "Jean-Paul Martin", "Managing Partner"),
])
def test_one_line_forms(line, name, title):
    (person,) = people_from_lines([line])
    assert (person.name, person.title) == (name, title)


def test_email_near_a_person():
    people = people_from_lines(["Mark Chen", "Office Manager", "mark.c@acme.com", "Something else"])
    assert people[0].email == "mark.c@acme.com"


@pytest.mark.parametrize("email,expected", [
    ("anna@smilestudio.com", "Anna Lopez"),
    ("anna.lopez@smilestudio.com", "Anna Lopez"),
    ("alopez@smilestudio.com", "Anna Lopez"),
    ("lopez@smilestudio.com", "Anna Lopez"),
    ("mark@smilestudio.com", "Mark Chen"),
    ("m.chen@smilestudio.com", "Mark Chen"),
    ("info@smilestudio.com", None),
    ("dr.lee@gmail.com", None),                   # no Lee on the website: left empty, not guessed
])
def test_match_person(email, expected):
    people = [Person("Anna Lopez", "Owner"), Person("Mark Chen", "Office Manager")]
    match = match_person(email, people)
    assert (match.name if match else None) == expected


def test_ambiguous_match_is_left_empty():
    people = [Person("Anna Lopez", "Owner"), Person("Anna Smith", "Hygienist")]
    assert match_person("anna@x.com", people) is None


@pytest.mark.parametrize("email,role", [
    ("info@x.com", "General inbox"), ("sales@x.com", "Sales"), ("billing@x.com", "Billing / Accounts"),
    ("hr@x.com", "HR / Hiring"), ("frontdesk@x.com", "Front desk / Office"), ("anna@x.com", None),
])
def test_inbox_designation(email, role):
    assert inbox_designation(email) == role


@pytest.mark.parametrize("title,dm", [
    ("Owner", True), ("Co-Founder", True), ("CEO", True), ("President", True), ("Managing Partner", True),
    ("Owner & Lead Dentist", True), ("Vice President of Sales", True), ("Office Manager", False),
    ("Associate Attorney", False), ("Dental Hygienist", False), (None, False),
])
def test_decision_maker(title, dm):
    assert is_decision_maker(title) is dm


def test_extract_page_includes_people():
    page = extract_page(fixture_html("about.html"), "https://smilestudio.com/about-us/")
    assert {p.name for p in page.people} >= {"Anna Lopez", "Mark Chen"}
    assert "mark@smilestudio.com" in page.emails


@pytest.mark.parametrize("people,business,domain,owner,basis", [
    ([Person("Mark Chen", "Office Manager"), Person("Anna Lopez", "Owner")], "Smile Studio", "smilestudio.com",
     "Anna Lopez", "title on website"),
    ([Person("Craig Larson", "Doctor")], "Larson Dental Boulder", "larsondentalboulder.com",
     "Craig Larson", "business named after them"),
    ([Person("John F. Weissman", "DDS"), Person("Devin", "Office Manager")], "Weissman Family Dental",
     "weissmanfamilydental.org", "John F. Weissman", "business named after them"),
    ([], "Dr. Ken Mickens DDS", "kenmickensdds.com", "Ken Mickens", "business named after them"),
    ([], "David L. Evans DDS - Cosmetic", "davidevansdds.com", "David L. Evans", "business named after them"),
    ([], "Boulder Smile Design", "bouldersmiledesign.com", None, None),      # not a person
    ([Person("Mark Chen", "Office Manager")], "Smile Studio", "smilestudio.com", None, None),
])
def test_owner_of(people, business, domain, owner, basis):
    from leadscout.crawler.people import owner_of
    found = owner_of(people, business, domain)
    assert (found[0].name, found[1]) == (owner, basis) if owner else found is None


def test_real_world_forms():
    from leadscout.crawler.people import find_people
    html = ("<p>Dr. Craig Larson has cared for Boulder families since 1998.</p>"
            "<p>Devin – Office Manager</p><p>Services - Owner Operated</p>")
    people = {p.name: p.title for p in find_people(html, BeautifulSoup(html, "lxml"))}
    assert people == {"Craig Larson": "Doctor", "Devin": "Office Manager"}
    assert match_person("craiglarsondds@gmail.com", [Person("Craig Larson", "Doctor")]).name == "Craig Larson"
    assert match_person("devin@x.com", [Person("Devin", "Office Manager")]).name == "Devin"


def test_business_names_are_not_people():
    html = ("<h1>Boulder Premier Dentistry, Dentist in Boulder, CO</h1>"
            "<p>Folsom Family Dentistry - Dentist in Boulder</p><p>JEFFREY PATRICIAN, DDS</p>")
    people = {p.name: p.title for p in find_people(html, BeautifulSoup(html, "lxml"))}
    assert people == {"Jeffrey Patrician": "DDS"}


def test_names_without_honorifics():
    assert Person("Dr. Daniel Alleman", "Lead Dentist and Owner").name == "Daniel Alleman"
    assert Person("Kyle Daniel, DDS").name == "Kyle Daniel"


@pytest.mark.parametrize("email,role", [
    ("drdanielfrontdesk@yahoo.com", "Front desk / Office"), ("financials@drwongortho.com", "Billing / Accounts"),
    ("smithbilling@x.com", "Billing / Accounts"), ("kirkorders@x.com", None), ("craiglarsondds@gmail.com", None),
])
def test_inbox_role_inside_address(email, role):
    assert inbox_designation(email) == role


def test_owner_named_business():
    from leadscout.crawler.people import owner_of
    found = owner_of([], "Kirk Rathburn", "rathburndental.com")
    assert (found[0].name, found[1]) == ("Kirk Rathburn", "business named after them")
    assert owner_of([], "Boulder Smile Design", "bouldersmiledesign.com") is None
    assert owner_of([], "Rock Pediatric Dentistry", "rockpediatricdentistry.com") is None


def test_address_of_someone_else_is_not_linked_by_position():
    people = people_from_lines(["Joe Guevara", "Vice President", "juancantu@lms.com"])
    assert people[0].email is None
    people = people_from_lines(["Joe Guevara", "Vice President", "jguevara@lms.com"])
    assert people[0].email == "jguevara@lms.com"


@pytest.mark.parametrize("title,level", [
    ("Owner", 1), ("President & Owner", 1), ("CEO", 1), ("Executive Director", 1),
    ("Irrigation Dept. Mgr & Owner", 1),
    ("School Board Vice-President", 2), ("VP of Sales", 2), ("Director of Operations", 2),
    ("General Manager", 2), ("Chief Financial Officer", 2),
    ("Office Manager", 3), ("Assistant Director", 4), ("Associate Attorney", 4), ("Dental Hygienist", 4),
    (None, None),
])
def test_seniority(title, level):
    from leadscout.crawler.people import seniority
    assert seniority(title) == level
    assert is_decision_maker(title) is (level is not None and level <= 2)


@pytest.mark.parametrize("text,name,title", [
    ("Rasmussen Sprinklers was founded in 1998 by Clifford Rasmussen.", "Clifford Rasmussen", "Founder"),
    ("A family business owned and operated by Maria Lopez.", "Maria Lopez", "Owner"),
    ("Meet our president, Tom Wu, who leads the crew.", "Tom Wu", "President"),
    ("Anna Reyes, owner of Reyes Roofing, says hi.", "Anna Reyes", "Owner"),
    ("Our Director: James O'Neil", "James O'Neil", "Director"),
])
def test_people_in_sentences(text, name, title):
    from leadscout.crawler.people import people_from_prose
    assert [(p.name, p.title) for p in people_from_prose(text)] == [(name, title)]


def test_sentences_without_a_person():
    from leadscout.crawler.people import people_from_prose
    assert people_from_prose("Founded by Two Brothers in 1980. Owned by Smith Family since 1950.") == []


def test_nickname_with_surname_matches():
    assert match_person("billhrncir@lms.com", [Person("Billy Hrncir", "President")]).name == "Billy Hrncir"
    assert match_person("juancantu@lms.com", [Person("Joe Guevara", "Vice President")]) is None


@pytest.mark.parametrize("business,domain", [
    ("Eagle Cold Storage", "eaglecoldstorage.com"), ("Dbi Rentals", "dbirentals.com"),
    ("Espazio Storage", "first-choiceselfstorage.com"), ("Kakatiya Farms", "kakatiyafarms.com"),
    ("Delina Chill", "delinachill.com"), ("Cubic Walls", "cubicwalls.com"),
])
def test_business_name_is_not_an_owner(business, domain):
    from leadscout.crawler.people import owner_of
    assert owner_of([], business, domain) is None


def test_business_is_not_one_of_its_people():
    from leadscout.crawler.people import not_the_business
    people = [Person("Mario's Welding", "Owner"), Person("Mario Diaz", "Owner"), Person("Spanish Homes", "Owner")]
    assert [p.name for p in not_the_business(people, "Mario's Welding LLC")] == ["Mario Diaz", "Spanish Homes"]
    assert [p.name for p in not_the_business(people[2:], "Spanish Homes Inc.")] == []


def test_all_caps_names_are_tidied():
    p = Person("PHILIP TURTURRO", "OWNER")
    assert (p.name, p.title) == ("Philip Turturro", "Owner")


@pytest.mark.parametrize("business", ["My Patio, LLC", "Spanish Homes Inc.", "Mario's Welding LLC"])
def test_company_suffix_is_not_a_credential(business):
    from leadscout.crawler.people import owner_of
    assert owner_of([], business, "example.com") is None
