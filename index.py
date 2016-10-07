import gzip
import json
import logging
import re
import sqlite3
from collections import Counter, namedtuple
from contextlib import contextmanager
from functools import lru_cache
from itertools import count

import lxml.etree
from PIL import Image

LineInfo = namedtuple('LineInfo', ('y_pos', 'height', 'sequence_pos'))
WordInfo = namedtuple('WordInfo', ('sequence_pos', 'start_x', 'end_x'))

logger = logging.getLogger(__name__)

SCHEMA = """
    CREATE TABLE IF NOT EXISTS transcriptions (
        id          INTEGER PRIMARY KEY,
        page_id     TEXT,
        document_id TEXT,
        text        TEXT,
        word_cuts   TEXT,
        position    INTEGER,
        pos_x       INTEGER,
        pos_y       INTEGER,
        width       INTEGER,
        height      INTEGER,
        UNIQUE(page_id, document_id, position) ON CONFLICT REPLACE
    );

    CREATE TABLE IF NOT EXISTS pages (
        id          INTEGER PRIMARY KEY,
        page_id     TEXT,
        document_id TEXT,
        img_path    TEXT,
        img_width   INTEGER,
        img_height  INTEGER,
        img_md5     INTEGER,
        UNIQUE(page_id, document_id) ON CONFLICT REPLACE
    );

    CREATE TABLE IF NOT EXISTS documents (
        id          INTEGER PRIMARY KEY,
        document_id TEXT UNIQUE,
        filename    TEXT UNIQUE,
        metadata    TEXT
    );

    CREATE TABLE IF NOT EXISTS lexica (
        id          INTEGER PRIMARY KEY,
        document_id TEXT UNIQUE,
        counter     BLOB
    );

    CREATE VIRTUAL TABLE text_idx USING fts5 (
        text,
        word_infos  UNINDEXED,
        page_id     UNINDEXED,
        document_id UNINDEXED,
        prefix='2 3',
        tokenize='porter unicode61 remove_diacritics 1'
    );
    CREATE VIRTUAL TABLE text_vocab USING fts5vocab(text_idx, col);
"""
INSERT_DOCUMENT = """
    INSERT INTO documents (document_id, filename, metadata)
        VALUES (:document_id, :filename, :metadata);
"""
INSERT_PAGE = """
    INSERT INTO pages (page_id, document_id, img_path, img_width, img_height,
                       img_md5)
        VALUES (:page_id, :document_id, :img_path, :img_width, :img_height,
                :img_md5);
"""
INSERT_TRANSCRIPTION = """
    INSERT INTO transcriptions (page_id, document_id, text, position,
                                word_cuts, pos_x, pos_y, width, height)
        VALUES (:page_id, :document_id, :text, :position, :word_cuts,
                :pos_x, :pos_y, :width, :height);
"""
SEARCH_INSIDE = """
    SELECT page_id, highlight(text_idx, 0, '<hi>', '</hi>'),
           word_infos, rank AS score FROM text_idx
        WHERE text_idx MATCH :query AND document_id = :document_id
        ORDER BY score
        LIMIT :limit;
"""
UPDATE_INDEX_SINGLE_DOCUMENT = """
    INSERT INTO text_idx (document_id, page_id, text, word_infos)
        SELECT document_id, page_id,
               group_concat(text, ' ') AS text,
               group_concat(word_infos, ' ') AS word_infos
        FROM (SELECT
                document_id, page_id, text,
                (word_cuts || '|' || pos_y ||
                 ':' || height || ':' || position || '||') AS word_infos
              FROM transcriptions
              WHERE document_id = :document_id
              ORDER BY page_id, position)
        GROUP BY page_id;
"""


class HocrDocument(object):
    def __init__(self, book_id, hocr_path):
        self.logger = logger.getChild('HocrDocument')
        self.id = book_id
        self.hocr_path = hocr_path
        parser = lxml.etree.XMLParser(ns_clean=True, recover=True)
        self.tree = lxml.etree.parse(str(self.hocr_path), parser)
        is_xhtml = len(self.tree.getroot().nsmap) > 0
        self.xpaths = {
            'page': ".//xhtml:div[@class='ocr_page']",
            'line': ".//xhtml:span[@class='ocr_line']",
            'word': ".//xhtml:span[@class='ocr_cinfo']"}
        if is_xhtml:
            self.nsmap = {'xhtml': 'http://www.w3.org/1999/xhtml'}
        else:
            self.xpaths = {k: xp.replace('xhtml:', '')
                           for k, xp in self.xpaths.items()}
            self.nsmap = None

    def _parse_title(self, title):
        if title is None:
            return {}
        return {itm.split(" ")[0]: " ".join(itm.split(" ")[1:])
                for itm in title.split("; ")}

    def _get_img_path(self, idx, title_data):
        if 'image' in title_data:
            return (self.hocr_path.parent / title_data['image']).resolve()
        # Google-Style
        img_path = self.hocr_path.parent / 'Image_{:04}.JPEG'.format(idx)
        try:
            return img_path.resolve()
        except OSError:
            raise ValueError("Could not determine image path")

    def get_pages(self):
        page_node_iter = self.tree.iterfind(self.xpaths['page'],
                                            namespaces=self.nsmap)
        for idx, page_node in enumerate(page_node_iter):
            title_data = self._parse_title(page_node.attrib.get('title'))
            try:
                img_path = self._get_img_path(idx, title_data)
            except ValueError:
                self.logger.debug(
                    "Could not find page image for page with id={} on book {}"
                    .format(page_node.attrib.get('id', idx), self.hocr_path))
                continue
            if 'bbox' not in title_data:
                dimensions = Image.open(str(img_path)).size
            else:
                dimensions = [
                    int(x) for x in title_data['bbox'].split()[2:]]
            page_id = page_node.attrib.get('id', 'page_{:04}'.format(idx))
            yield page_id, dimensions, img_path, title_data.get('imagemd5')

    def get_lines(self):
        page_node_iter = self.tree.iterfind(self.xpaths['page'],
                                            namespaces=self.nsmap)
        for idx, page_node in enumerate(page_node_iter):
            page_id = page_node.attrib.get('id', 'page_{:04}'.format(idx))
            lines = []
            line_nodes = page_node.iterfind(self.xpaths['line'],
                                            namespaces=self.nsmap)
            word_idx_gen = (str(v) for v in count())
            for line_node in line_nodes:
                title_data = self._parse_title(
                    line_node.attrib.get('title'))
                if 'bbox' not in title_data:
                    self.logger.debug(
                        "Could not determine bbox for line with id={} "
                        "on page with id={} on book {}"
                        .format(line_node.attrib.get('id'), page_id,
                                self.hocr_path))
                    bbox = (None, None, None, None)
                else:
                    bbox = [int(v) for v in title_data['bbox'].split()]
                word_nodes = line_node.iterfind(
                    self.xpaths['word'], namespaces=self.nsmap)
                word_cuts = []
                for word_node in word_nodes:
                    title_data = self._parse_title(
                        word_node.attrib.get('title'))
                    if title_data:
                        word_bbox = title_data['bbox'].split()
                        word_cuts.append((next(word_idx_gen), word_bbox[0],
                                          word_bbox[2]))
                    else:
                        word_cuts.append((next(word_idx_gen), "-1", "-1"))
                text = re.sub(r'\s{2,}', ' ',
                              "".join(line_node.itertext()).strip())
                if text:
                    lines.append((bbox, word_cuts, text))
            yield page_id, lines


class DocumentRepository(object):
    def __init__(self, db_path):
        """ Local document index using SQLite.

        :param db_path: Path to the database file
        :type db_path:  :py:class:`pathlib.Path`
        """
        init_db = not db_path.exists()
        self.db_path = db_path
        if init_db:
            with self._db as cur:
                cur.executescript(SCHEMA)

    @property
    @contextmanager
    def _db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            yield cursor

    @lru_cache(5)
    def _get_term_frequencies(self, document_id):
        with self._db as cur:
            return Counter(json.loads(gzip.decompress(
                cur.execute("SELECT counter FROM lexica WHERE document_id = ?",
                            (document_id,)).fetchone()[0]).decode('utf8')))

    def document_ids(self):
        with self._db as cur:
            return (
                r[0] for r in
                cur.execute("SELECT document_id FROM documents").fetchall())

    def get_document(self, document_id):
        with self._db as cur:
            return cur.execute(
                "SELECT document_id, filename, metadata FROM documents "
                "WHERE document_id = ?", (document_id,)).fetchone()

    def get_image_path(self, document_id, page_id):
        with self._db as cur:
            return cur.execute(
                "SELECT img_path FROM pages "
                "WHERE document_id = ? AND  page_id = ?",
                (document_id, page_id)).fetchone()[0]

    def get_lines(self, document_id, page_id):
        with self._db as cur:
            return cur.execute(
                "SELECT text, pos_x, pos_y, width, height FROM transcriptions "
                "WHERE document_id = ? AND page_id = ? "
                "ORDER BY position",
                (document_id, page_id)).fetchall()

    def get_pages(self, document_id):
        with self._db as cur:
            return cur.execute(
                "SELECT page_id, img_path, img_width, img_height "
                "FROM pages "
                "WHERE document_id = ? ORDER BY page_id",
                (document_id,)).fetchall()

    def get_page(self, document_id, page_id):
        with self._db as cur:
            return cur.execute(
                "SELECT page_id, img_path, img_width, img_height "
                "FROM pages "
                "WHERE document_id = ? AND page_id = ?",
                (document_id, page_id)).fetchone()

    def ingest_document(self, hocr_path, autocomplete_min_count=5):
        """ Ingest a new document.

        :param hocr_path:   path to load document from
        :type lines:        :py:class:`pathlib.Path`
        """
        doc_id = hocr_path.stem
        if doc_id == 'hOCR':
            # For Google Books dataset
            doc_id = hocr_path.parent.stem
        doc = HocrDocument(doc_id, hocr_path)
        with self._db as cur:
            cur.execute(
                INSERT_DOCUMENT,
                dict(document_id=doc_id, filename=str(hocr_path),
                     metadata=None))
            for page_id, dimensions, img_path, md5sum in doc.get_pages():
                cur.execute(
                    INSERT_PAGE,
                    dict(page_id=page_id, document_id=doc_id,
                         img_path=str(img_path), img_width=dimensions[0],
                         img_height=dimensions[1], img_md5=md5sum))

            for page_id, lines in doc.get_lines():
                line_vals = (
                    dict(document_id=doc_id, page_id=page_id,
                         text=line_text, position=pos,
                         word_cuts=" ".join(':'.join(c) for c in word_cuts),
                         pos_x=x1, pos_y=y1,
                         width=x2-x1 if x1 and x2 else None,
                         height=y2-y1 if x1 and x2 else None)
                    for pos, ((x1, y1, x2, y2), word_cuts, line_text)
                    in enumerate(lines))
                cur.executemany(INSERT_TRANSCRIPTION, line_vals)
            self._update_search_index(doc_id, autocomplete_min_count)

    def _update_search_index(self, doc_id, autocomplete_min_count):
        # FIXME: This is a bit unwiedly and I'd prefer there was a nicely
        #        scalable in-SQL solution, but unfortunately keeping the
        #        term frequencies for each document in a table makes
        #        the database size explode, so gzipped json-dumped counters
        #        it is for now :/
        with self._db as cur:
            terms_before = Counter(dict(
                cur.execute("SELECT term, cnt FROM text_vocab").fetchall()))
            cur.execute(UPDATE_INDEX_SINGLE_DOCUMENT, {'document_id': doc_id})
            terms_after = Counter(dict(
                cur.execute("SELECT term, cnt FROM text_vocab").fetchall()))
            doc_terms = Counter(dict(
                (term, cnt_after - terms_before.get('term', 0))
                for term, cnt_after in terms_after.items()
                if cnt_after != terms_before.get('term')))
            # Purge terms below threshold to save on size
            to_purge = []
            for term, cnt in doc_terms.items():
                if cnt < autocomplete_min_count:
                    to_purge.append(term)
            for term in to_purge:
                del doc_terms[term]
            cur.execute(
                "INSERT INTO lexica (document_id, counter) VALUES (?, ?)",
                (doc_id, gzip.compress(json.dumps(doc_terms).encode('utf8'))))

    def search(self, query, document_id, limit=50):
        """ Search the index for pages matching the query.

        :param query:   A SQLite FTS5 query
        :param document_id:     Restrict search to this document
        :param limit:   Maximum number of matches to return
        :returns:       Generator that yields matches with their coordinates
        """
        with self._db as cur:
            matches = cur.execute(SEARCH_INSIDE, {'document_id': document_id,
                                                  'query': query,
                                                  'limit': limit}).fetchall()
        for page_id, match_text, word_infos, score in matches:
            line_infos = []
            for combined in word_infos.split('||'):
                if not combined:
                    continue
                winfos, linfo = combined.split('|')
                linfo = LineInfo(*(int(x) if x != '' else -1
                                   for x in linfo.split(':')))
                winfos = tuple(
                    WordInfo(*(int(x) if x != '' else -1
                               for x in w.split(':')))
                    for w in winfos.split(' ') if w)
                line_infos.append((linfo, winfos))
            yield page_id, match_text, line_infos

    def autocomplete(self, query, document_id, min_cnt=1):
        query = query.lower()
        freqs = self._get_term_frequencies(document_id)
        return ((term, freq) for term, freq in freqs.most_common()
                if term.startswith(query) and freq >= min_cnt)
