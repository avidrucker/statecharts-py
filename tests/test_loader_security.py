"""Regression tests for #43 — the SCXML loader must reject a DTD internal subset.

Graduated from the SCP-C-059 Claim test (code-review finding §3.2). Keeps its Claim ID in the
docstring for verify-claims ledger parity.

SAFETY (ticket #43, core requirement): the payload below is deliberately bounded — 4 entity
levels, ~10 KB total expansion. It must NOT be deepened. These tests run RED before the guard
exists, and RED means `load_string` really does parse and expand the payload; an unbounded
"billion laughs" here would amplify for real and can OOM the machine running the suite. The
guard fires on the *presence* of the DOCTYPE, before expansion, so a shallow entity proves
rejection exactly as well as a deep one.
"""

BOUNDED_ENTITY_DOC = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE scxml [\n"
    ' <!ENTITY a "aaaaaaaaaa">\n'
    ' <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
    ' <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
    ' <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">\n'
    "]>\n"
    '<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0" initial="s">\n'
    '  <state id="s"><onentry><log expr="&d;"/></onentry></state>\n'
    "</scxml>"
)


def test_scp_c_059_loader_rejects_internal_entity_dtd():
    """SCP-C-059: statecharts' SCXML loader (`load_string` in `scxml/loader.py`) parses with
    `xml.etree.ElementTree.fromstring`, which expands internal XML entities, so a nested-entity
    ('billion laughs') document is accepted and amplifies input exponentially. Asserts the
    CORRECT (hardened) behavior — the loader must reject a document carrying a DTD internal
    subset, raising `InsecureDocument` before any expansion. RED on `0c7776f` (the document is
    accepted and `&d;` expands 28x to 10,000 bytes); GREEN once the DOCTYPE is refused."""
    from statecharts.scxml.loader import InsecureDocument, load_string

    try:
        load_string(BOUNDED_ENTITY_DOC)
    except InsecureDocument:
        return
    raise AssertionError(
        "loader accepted a document with a DTD internal subset (billion-laughs exposure)"
    )


def test_loader_accepts_prolog_without_doctype():
    """The DOCTYPE guard must not over-reject: a document whose prolog carries the XML
    declaration, a comment, and a processing instruction — every construct legal before the
    root element — still loads. Guards the prolog scanner against rejecting valid SCXML."""
    from statecharts.scxml.loader import load_string

    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- a comment, which may itself mention <!DOCTYPE without being one -->\n"
        '<?pi-target some instruction?>\n'
        '<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0" initial="s">\n'
        '  <state id="s"/>\n'
        "</scxml>"
    )
    root, meta = load_string(doc)
    assert root.id == "scxml"
    assert meta["binding"] == "early"
