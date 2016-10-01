import logging
import re
import sqlite3
from contextlib import contextmanager

import lxml.etree
from PIL import Image

NAMESPACES = {'xhtml': 'http://www.w3.org/1999/xhtml'}
XPATH_PAGE = ".//xhtml:div[@class='ocr_page']"
XPATH_LINE = ".//xhtml:span[@class='ocr_line']"
XPATH_WORD = ".//xhtml:span[@class='ocr_cinfo']"

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

    CREATE VIRTUAL TABLE text_idx USING fts5 (
        text,
        word_infos  UNINDEXED,
        page_id     UNINDEXED,
        document_id UNINDEXED,
        prefix='2,3',
        tokenize='porter unicode61 remove_diacritics 1'
    );
    CREATE VIRTUAL TABLE text_idx_vocab USING fts5vocab(text_idx, row);
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
    SELECT page_id, document_id, text, word_infos, rank AS score FROM text_idx
        WHERE text_idx MATCH :query AND document_id = :document_id
        LIMIT :limit
        ORDER BY score;
"""
UPDATE_INDEX_FULL = """
    INSERT INTO text_idx (document_id, page_id, text, word_infos)
        SELECT document_id, page_id,
               group_concat(text, ' ') AS text,
               group_concat(word_infos, ' ') AS word_infos
        FROM (SELECT
                document_id, page_id, text,
                (word_cuts || ':' || pos_y ||
                 ':' || height || ':' || position) AS word_infos
              FROM transcriptions
              ORDER BY document_id, page_id, position)
        GROUP BY document_id, page_id;
"""
UPDATE_INDEX_SINGLE_DOCUMENT = """
    INSERT INTO text_idx (page_id, text, word_infos)
        SELECT page_id,
               group_concat(text, ' ') AS text,
               group_concat(word_infos, ' ') AS word_infos
        FROM (SELECT
                page_id, text,
                (word_cuts || ':' || pos_y ||
                 ':' || height || ':' || position) AS word_infos
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
        self.tree = lxml.etree.parse(str(self.hocr_path))

    def _parse_title(self, title):
        if title is None:
            return
        return {itm.split(" ")[0]: " ".join(itm.split(" ")[1:])
                for itm in title.split("; ")}

    def get_pages(self):
        for page_node in self.tree.iterfind(XPATH_PAGE, namespaces=NAMESPACES):
            title_data = self._parse_title(page_node.attrib.get('title'))
            if title_data is None or 'image' not in title_data:
                self.logger.debug(
                    "Could not parse title data for page with id={} "
                    "on book {}"
                    .format(page_node.attrib.get('id'), self.hocr_path))
                continue
            img_path = (self.hocr_path.parent /
                        title_data['image']).resolve()
            if 'bbox' not in title_data:
                dimensions = Image.open(str(img_path)).size
            else:
                dimensions = [
                    int(x) for x in title_data['bbox'].split()[2:]]
            page_id = page_node.attrib['id']
            yield page_id, dimensions, img_path, title_data.get('imagemd5')

    def get_lines(self):
        for page_node in self.tree.iterfind(XPATH_PAGE, namespaces=NAMESPACES):
            page_id = page_node.attrib.get('id')
            lines = []
            line_nodes = page_node.iterfind(XPATH_LINE, namespaces=NAMESPACES)
            for line_node in line_nodes:
                title_data = self._parse_title(
                    line_node.attrib.get('title'))
                if title_data is None:
                    self.logger.debug(
                        "Could not parse title data for line with id={} "
                        "on page with id={} on book {}"
                        .format(line_node.attrib.get('id'), page_id,
                                self.hocr_path))
                    bbox = (None, None, None, None)
                else:
                    bbox = [int(v) for v in title_data['bbox'].split()]
                word_nodes = line_node.iterfind(
                    XPATH_WORD, namespaces=NAMESPACES)
                word_cuts = []
                for word_node in word_nodes:
                    title_data = self._parse_title(
                        word_node.attrib.get('title'))
                    if title_data:
                        word_bbox = title_data['bbox'].split()
                        word_cuts.append((word_bbox[0], word_bbox[2]))
                    else:
                        word_cuts.append(("-1", "-1"))
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

    def ingest_document(self, hocr_path):
        """ Ingest a new document.

        :param hocr_path:   path to load document from
        :type lines:        :py:class:`pathlib.Path`
        """
        doc_id = hocr_path.stem
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
            cur.execute(UPDATE_INDEX_SINGLE_DOCUMENT, {'document_id': doc_id})

    def search(self, query, document_id=None, limit=None):
        """ Search the index for pages matching the query.

        :param query:   A SQLite FTS5 query
        :param document_id:     Restrict search to this document
        :param limit:   Maximum number of matches to return
        :returns:       Generator that yields matches with their coordinates
        """
        # TODO: Retrieve matching highlighted lines along with their page_id,
        #       document_id and word_infos from index
        # TODO: Determine the start and end index of the highlighted match
        # TODO: Obtain word_infos for all words in range
        # TODO: Get matching line positions along with the matching words on
        #       that line and their coordinates
        raise NotImplementedError

    def autocomplete(self, query, limit=50):
        with self._db as cur:
            return cur.execute(
                "SELECT term FROM text_idx_vocab "
                "WHERE term LIKE ? "
                "ORDER BY cnt DESC LIMIT ?", (query + '%', limit)).fetchall()
