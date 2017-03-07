from isbnlib import mask, to_isbn13
import re
from functools import partial
import os, sys, json, copy, calendar
from et3.render import render, doall, EXCLUDE_ME
from et3.extract import lookup as p
from et3.utils import requires_context
from elifetools import parseJATS
from functools import wraps
import logging
from collections import OrderedDict
from datetime import datetime
from slugify import slugify
import conf, utils, glencoe
import validate

LOG = logging.getLogger(__name__)
_handler = logging.FileHandler('scrape.log')
_handler.setLevel(logging.INFO)
_handler.setFormatter(conf._formatter)
LOG.addHandler(_handler)

#
# utils
#

def video_msid(msid):
    """Replaces the msid of testing articles with the reference one they were generated from.

    Leaves real articles untouched"""
    if int(msid) > 100000:
        return utils.pad_msid(str(msid[-5:]))
    return msid

def doi(item):
    return parseJATS.doi(item)

def to_isoformat(time_struct):
    if not time_struct:
        return time_struct
    # time_struct ll: time.struct_time(tm_year=2015, tm_mon=9, tm_mday=10, tm_hour=0, tm_min=0, tm_sec=0, tm_wday=3, tm_yday=253, tm_isdst=0)
    ts = calendar.timegm(time_struct) # ll: 1441843200
    ts = datetime.utcfromtimestamp(ts) # datetime.datetime(2015, 9, 10, 0, 0)
    return utils.ymdhms(ts)

def is_poa_to_status(is_poa):
    return "poa" if is_poa else "vor"

def to_soup(doc):
    if isinstance(doc, basestring):
        if os.path.exists(doc):
            return parseJATS.parse_document(doc)
        return parseJATS.parse_xml(doc)
    # assume it's a file-like object and attempt to .read() it's contents
    return parseJATS.parse_xml(doc.read())

def jats(funcname, *args, **kwargs):
    aliases = {
        'msid': 'publisher_id',
    }
    actual_func = getattr(parseJATS, funcname, None) or getattr(parseJATS, aliases.get(funcname))
    if not actual_func:
        raise ValueError("you asked for %r from parseJATS but I couldn't find it!" % funcname)

    @wraps(actual_func)
    def fn(soup):
        return actual_func(soup, *args, **kwargs)
    return fn

#
#
#


DISPLAY_CHANNEL_TYPES = {
    "Correction": "correction",
    "Editorial": "editorial",
    "Feature Article": "feature",
    "Feature article": "feature",
    "Insight": "insight",
    "Registered Report": "registered-report",
    "Registered report": "registered-report",
    "Research Advance": "research-advance",
    "Research Article": "research-article",
    "Research article": "research-article",
    "Short report": "short-report",
    "Short Report": "short-report",
    "Tools and Resources": "tools-resources",
    "Replication Study": "replication-study",
    "Replication study": "replication-study",

    # NOTE: have not seen the below ones yet, guessing
    "Research exchange": "research-exchange",
    "Retraction": "retraction",
}

def display_channel_to_article_type(display_channel_list):
    if not display_channel_list:
        LOG.warn("type: display channel list not provided")
        return
    display_channel = display_channel_list[0]
    retval = DISPLAY_CHANNEL_TYPES.get(display_channel)
    if not retval:
        LOG.warn("type: given value %r has no mention in idx: %s", display_channel, DISPLAY_CHANNEL_TYPES.keys())
    return retval

LICENCE_TYPES = {
    "http://creativecommons.org/licenses/by/3.0/": "CC-BY-3.0",
    "http://creativecommons.org/licenses/by/4.0/": "CC-BY-4.0",
    "http://creativecommons.org/publicdomain/zero/1.0/": "CC0-1.0"
}

def related_article_to_related_articles(related_article_list):
    # ll: [{'xlink_href': u'10.7554/eLife.09561', 'related_article_type': u'article-reference', 'ext_link_type': u'doi'}]
    def et(struct):
        return struct.get('xlink_href', '').rsplit('.', 1)[-1] or None
    # ll: ['09561'] or None
    return filter(None, map(et, related_article_list))

def mixed_citation_to_related_articles(mixed_citation_list):
    # ll: [{'article': {'authors': [{'given': u'R', 'surname': u'Straussman'}, ...}],
    #      'doi': u'10.1038/nature11183', 'pub-date': [2014, 2, 28], 'title': u'Pants-Party'},
    #      'journal': {'volume': u'487', 'lpage': u'504', 'name': u'Nature', 'fpage': u'500'}}]
    def authorline(a):
        return '- %s %s' % (a['given'], a['surname'])

    def et(struct):
        return {
            'type': 'external-article',
            'articleTitle': p(struct, 'article.title'),
            'journal': p(struct, 'journal.name'),
            'authorLine': '\n'.join(map(authorline, p(struct, 'article.authors'))),
            'uri': 'https://doi.org/%s' % p(struct, 'article.doi'),
        }
    return map(et, mixed_citation_list)

def cdnlink(msid, filename):
    kwargs = {
        'padded-msid': utils.pad_msid(msid),
        'fname': filename
    }
    return conf.CDN % kwargs

def base_url(msid):
    return cdnlink(msid, '')

def pdf_uri(triple):
    """predict an article's pdf url.
    some article types don't have a PDF (like corrections) and some
    older articles that should have a pdf, don't. this function doesn't
    concern itself with those latter exceptions."""
    content_type, msid, version = triple
    if content_type and True in map(lambda type: type in ['Correction'], content_type):
        return EXCLUDE_ME
    filename = "elife-%s-v%s.pdf" % (utils.pad_msid(msid), version) # ll: elife-09560-v1.pdf
    return cdnlink(msid, filename)

def category_codes(cat_list):
    subjects = []
    for cat in cat_list:
        subject = OrderedDict()
        subject['id'] = slugify(cat, stopwords=['and'])
        subject['name'] = cat
        subjects.append(subject)
    return subjects

def handle_isbn(val):
    return mask(to_isbn13(str(val)))

def to_volume_correct(pair):
    pub_date, volume = pair
    if not volume:
        # no volume on unpublished PoA articles, calculate based on year published
        if isinstance(pub_date, basestring):
            # assume yyyy-mm-dd formatted string
            pub_year = int(pub_date[:4])
        else:
            # assume a timestruct
            pub_year = pub_date[0]  # to_isoformat(pub_date)[:4]
        volume = pub_year - (conf.JOURNAL_INCEPTION - 1) # 2011 for elife
    return int(volume)

def to_volume_incorrect(pair):
    pub_date, volume = pair
    if not volume:
        # no volume on unpublished PoA articles, calculate based on current year
        # (preserving behaviour in EIF generation)
        year = datetime.now().year
        volume = year - (conf.JOURNAL_INCEPTION - 1) # 2011 for elife
    return int(volume)

to_volume = to_volume_incorrect

@requires_context
def discard_if_not_v1(ctx, ver):
    "discards given value if the version of the article being worked on is not a v1"
    if ctx['version'] == 1:
        return ver
    return EXCLUDE_ME

def getvar(varname):
    @requires_context
    def fn(ctx, _):
        return ctx[varname]
    return fn

'''
def discard_if(pred): # can also be used like: discard_if(None)
    def fn(v):
        if pred is None:
            return EXCLUDE_ME
        return EXCLUDE_ME if pred(v) else v
    return fn
'''

def discard_if_none_or_empty(v):
    if not v:
        return EXCLUDE_ME
    elif len(v) <= 0:
        return EXCLUDE_ME
    return v

def discard_if_none_or_cc0(pair):
    holder, licence = pair
    if not holder or str(licence).upper().startswith('CC0-'):
        return EXCLUDE_ME
    return holder

def body(soup):
    return jats('body_json', base_url(jats('publisher_id')(soup)))(soup)

def appendices(soup):
    return jats('appendices_json', base_url(jats('publisher_id')(soup)))(soup)

#
# post processing
#

def visit(data, pred, fn, coll=None):
    "visits every value in the given data and applies `fn` when `pred` is true "
    if pred(data):
        if coll is not None:
            data = fn(data, coll)
        else:
            data = fn(data)
        # why don't we return here after matching?
        # the match may contain matches within child elements (lists, dicts)
        # we want to visit them, too
    if isinstance(data, OrderedDict):
        results = OrderedDict()
        for key, val in data.items():
            results[key] = visit(val, pred, fn, coll)
        return results
    elif isinstance(data, dict):
        return {key: visit(val, pred, fn, coll) for key, val in data.items()}
    elif isinstance(data, list):
        return [visit(row, pred, fn, coll) for row in data]
    # unsupported type/no further matches
    return data


def expand_videos(data):
    msid = data['snippet']['id']

    def pred(element):
        return isinstance(element, dict) and element.get("type") == "video"

    return visit(data, pred, partial(glencoe.expand_videos, video_msid(msid)))

def expand_uris(msid, data):
    "any 'uri' element is given a proper cdn link"

    protocol_matcher = re.compile(r'(http|ftp)s?:\/\/.*')

    def pred(element):
        # dictionary with 'uri' key exists that hasn't been expanded yet
        return isinstance(element, dict) \
            and "uri" in element \
            and not protocol_matcher.match(element["uri"])

    def fn(element):
        uri = element["uri"]
        # edge case: 'www' without a protocol
        if uri.startswith('www'):
            # all urls must have a protocol.
            # this should have been picked up in the bot or in production.
            fixed = 'http://' + element['uri']
            LOG.warn("broken url: %r has become %r" % (uri, fixed))
            element['uri'] = fixed
            return element
        # edge case: 'doi:' is not a protocol
        if uri.startswith('doi:'):
            fixed = 'https://doi.org/' + uri[4:]
            LOG.warn("broken url: %r has become %r" % (uri, fixed))
            element['uri'] = fixed
            return element
        # normal case: cdn link
        element["filename"] = os.path.basename(element["uri"]) # basename here redundant?
        element["uri"] = cdnlink(msid, element["uri"])
        return element
    return visit(data, pred, fn)

def fix_extensions(data):
    "in some older articles there are uris with no file extensions. call before expand_uris"

    # 15852
    def pred(element):
        return isinstance(element, dict) \
            and element.get("type") == "image" \
            and not os.path.splitext(element["uri"])[1] # ext in pair of (fname, ext) is empty

    def fn(element, missing):
        missing.append(utils.subdict(element, ['type', 'id', 'uri']))
        element["uri"] += ".jpg"
        return element

    missing = []
    data = visit(data, pred, fn, missing)

    if missing and 'snippet' in data: # test cases rarely have a 'snippet' in them
        context = {
            'msid': data['snippet']['id'],
            'version': data['snippet']['version'],
            'missing': missing
        }
        LOG.info("encountered article with %s images with missing file extensions. assuming .jpg", len(missing), extra=context)

    return data

def prune(data):
    prune_if_none = [
        "pdf", "relatedArticles", "digest", "abstract", "titlePrefix",
        "acknowledgements"
    ]
    prune_if_empty = [
        "impactStatement", "decisionLetter", "authorResponse",
        "researchOrganisms", "keywords", "references",
        "ethics", "appendices", "dataSets", "additionalFiles",
        "funding"
    ]
    empty = [[], {}, ""]

    def pred(element):
        # visit any element that contains any of the above keys
        return isinstance(element, dict) and utils.contains_any(element, prune_if_none + prune_if_empty)

    def fn(element):
        element = utils.rmkeys(element, prune_if_none, lambda val: val is None)
        element = utils.rmkeys(element, prune_if_empty, lambda val: val in empty)
        return element
    return visit(data, pred, fn)

def format_isbns(data):
    def pred(element):
        return isinstance(element, dict) and 'isbn' in element

    def fn(element):
        element['isbn'] = handle_isbn(element['isbn'])
        return element

    return visit(data, pred, fn)

def postprocess(data):
    msid = data['snippet']['id']
    data = doall(data, [
        fix_extensions,
        expand_videos,
        partial(expand_uris, msid),
        format_isbns,
        prune
    ])
    return data
#
#
#

JOURNAL = OrderedDict([
    ('id', [jats('journal_id')]),
    ('title', [jats('journal_title')]),
    ('issn', [jats('journal_issn', 'electronic')]),
])

SNIPPET = OrderedDict([
    ('-meta', OrderedDict([
        ('location', [getvar('location')]),
    ])),
    ('status', [jats('is_poa'), is_poa_to_status]),
    ('id', [jats('publisher_id')]),
    ('version', [getvar('version')]),
    ('type', [jats('display_channel'), display_channel_to_article_type]),
    ('doi', [jats('doi')]),
    ('authorLine', [jats('author_line'), discard_if_none_or_empty]),
    ('title', [jats('full_title_json')]),
    ('titlePrefix', [jats('title_prefix')]),
    ('published', [jats('pub_date'), to_isoformat]), # 'published' is the pubdate of the v1 article
    ('versionDate', [jats('pub_date'), to_isoformat, discard_if_not_v1]), # date *this version* published. provided by Lax.
    ('volume', [(jats('pub_date'), jats('volume')), to_volume]),
    ('elocationId', [jats('elocation_id')]),
    ('pdf', [(jats('display_channel'), jats('publisher_id'), getvar('version')), pdf_uri]),
    ('subjects', [jats('category'), category_codes]),
    ('researchOrganisms', [jats('research_organism_json')]),
    ('abstract', [jats('abstract_json')]),
])
# https://github.com/elifesciences/api-raml/blob/develop/dist/model/article-poa.v1.json#L689
POA_SNIPPET = copy.deepcopy(SNIPPET)

# a POA contains the contents of a POA snippet
POA = copy.deepcopy(POA_SNIPPET)
POA.update(OrderedDict([
    ('copyright', OrderedDict([
        ('license', [jats('license_url'), LICENCE_TYPES.get]),
        ('holder', [(jats('copyright_holder'), jats('license')), discard_if_none_or_cc0]),
        ('statement', [jats('license')]),
    ])),
    ('authors', [jats('authors_json'), discard_if_none_or_empty]),
    ('ethics', [jats('ethics_json')]),
    ('funding', OrderedDict([
        ('awards', [jats('funding_awards_json'), discard_if_none_or_empty]),
        ('statement', [jats('funding_statement_json'), discard_if_none_or_empty]),
    ])),
    ('additionalFiles', [jats('supplementary_files_json')]),
    ('dataSets', [jats('datasets_json')]),
]))

# a VOR snippets contains the contents of a POA
VOR_SNIPPET = copy.deepcopy(POA)
VOR_SNIPPET.update(OrderedDict([
    ('impactStatement', [jats('impact_statement_json')]),
]))

# a VOR contains the contents of a VOR snippet
VOR = copy.deepcopy(VOR_SNIPPET)
VOR.update(OrderedDict([
    ('keywords', [jats('keywords_json')]),
    ('-related-articles-internal', [jats('related_article'), related_article_to_related_articles]),
    ('-related-articles-external', [jats('mixed_citations'), mixed_citation_to_related_articles]),
    ('digest', [jats('digest_json')]),
    ('body', [body]),
    ('references', [jats('references_json')]),
    ('appendices', [appendices]),
    ('acknowledgements', [jats('acknowledgements_json')]),
    ('decisionLetter', [jats('decision_letter')]),
    ('authorResponse', [jats('author_response')]),
]))

def mkdescription(poa=True):
    "returns the description to scrape based on the article type"
    return OrderedDict([
        ('journal', JOURNAL),
        ('snippet', POA_SNIPPET if poa else VOR_SNIPPET),
        ('article', POA if poa else VOR),
    ])

#
# bootstrap
#

def expand_location(path):
    if isinstance(path, file):
        path = doc.name

    elif os.path.exists(path):
        # so we always have an absolute path
        path = os.path.join(conf.PROJECT_DIR, path)

    else:
        # just ensure we have a string to work with
        path = path or ''

    if re.match(r".*article-xml/articles/.+\.xml$", path):
        # this article is coming from the local ./article-xml/ directory, which
        # is almost certainly a git checkout. we want a location that looks like:
        # https://raw.githubusercontent.com/elifesciences/elife-article-xml/5f1179c24c9b8a8b700c5f5bf3543d16a32fbe2f/articles/elife-00003-v1.xml
        rc, rawsha = utils.run_script(["cat", "elife-article-xml.sha1"])
        utils.ensure(rc == 0, "failed to read the contents of './elife-article-xml.sha1'")
        sha = rawsha.strip()
        fname = os.path.basename(path)
        return "https://raw.githubusercontent.com/elifesciences/elife-article-xml/%s/articles/%s" % (sha, fname)

    elif path.startswith('https://s3.amazonaws.com'):
        # it's being downloaded from a bucket, no worries
        return path

    # who knows what this path is ...
    LOG.warn("scraping article content in a non-repeatable way. please don't send the results to lax")
    return path

def render_single(doc, **ctx):
    try:
        # passing a 'location' value will override pulling the value from the doc
        ctx['location'] = expand_location(ctx.get('location', doc))
        soup = to_soup(doc)
        description = mkdescription(parseJATS.is_poa(soup))
        article_data = postprocess(render(description, [soup], ctx)[0])

        if conf.PATCH_AJSON_FOR_VALIDATION: # makes in-place changes to the data
            validate.add_placeholders_for_validation(article_data)
            LOG.debug("placeholders attached")

        return article_data

    except Exception as err:
        LOG.error("failed to render doc with error: %s", err)
        raise

def main(doc):
    msid, version = utils.version_from_path(getattr(doc, 'name', doc))
    try:
        article_json = render_single(doc, version=version)
        return json.dumps(article_json, indent=4)
    except Exception:
        LOG.exception("failed to scrape article", extra={'doc': doc, 'msid': msid, 'version': version})
        raise

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('infile', nargs="?", type=argparse.FileType('r'), default=sys.stdin)
    parser.add_argument('--verbose', action="store_true", default=False)
    args = parser.parse_args()
    doc = args.infile
    print main(doc)
