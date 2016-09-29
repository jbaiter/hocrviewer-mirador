from __future__ import print_function

import logging
import os
import sys
from collections import OrderedDict
from multiprocessing import cpu_count

import click
import click_log
import flask
import gunicorn.app.base
import lxml.etree
from flask_iiif import IIIF
from flask_iiif.cache.simple import ImageSimpleCache
from flask_restful import Api
from iiif_prezi.factory import ManifestFactory
from PIL import Image

NAMESPACES = {'xhtml': 'http://www.w3.org/1999/xhtml'}
XPATH_PAGE = ".//xhtml:div[@class='ocr_page']"
XPATH_LINE = ".//xhtml:span[@class='ocr_line']"
XPATH_WORD = ".//xhtml:span[@class='ocr_cinfo']"


app = flask.Flask('hocrviewer', static_folder='vendor/mirador/build/mirador',
                  static_url_path='/static')
ext = IIIF(app=app)
api = Api(app=app)
ext.init_restful(api, prefix="/iiif/image/")
repository = None
logger = logging.getLogger(__name__)


class HocrRepository(object):
    cache = {}

    def __init__(self, base_directory, init_cache=True):
        self.logger = logger.getChild('HocrRepository')
        self.base_dir = base_directory
        self.logger.info("Initializing book cache")
        book_ids = [p[:-5] for p in os.listdir(base_directory)
                    if p.endswith('.html')]
        for idx, book_id in enumerate(book_ids):
            self.logger.debug(
                "Read {} ({}/{})".format(book_id, idx, len(book_ids)))
            try:
                self.get(book_id)
            except:
                logger.warn("Could not parse {}".format(book_id))

    def get(self, book_id):
        if book_id in self.cache:
            book = self.cache[book_id]
        else:
            self.logger.debug('Cache miss, reading HOCR from disk')
            hocr_path = os.path.join(self.base_dir, book_id + '.html')
            if not os.path.exists(hocr_path):
                self.logger.warn('HOCR file {} does not exist.'
                                 .format(hocr_path))
                return None
            book = HocrDocument(book_id, hocr_path)
            self.cache[book_id] = book
        return book

    def book_ids(self):
        return [p[:-5] for p in os.listdir(self.base_dir)
                if p.endswith('.html')]


class HocrDocument(object):
    def __init__(self, book_id, hocr_path):
        self.logger = logger.getChild('HocrDocument')
        self.id = book_id
        self.hocr_path = hocr_path
        self._images = OrderedDict()
        self._lines = OrderedDict()

    def _parse_title(self, title):
        if title is None:
            return
        return {itm.split(" ")[0]: " ".join(itm.split(" ")[1:])
                for itm in title.split("; ")}

    def get_image_path(self, page_id):
        if not self._images:
            list(self.get_images())
        return self._images[page_id][1]

    def get_images(self):
        if self._images:
            for page_id, data in self._images.items():
                dimensions, fpath = data
                yield page_id, dimensions, fpath
        else:
            self.logger.debug("Cache miss while getting images for {}"
                              .format(self.hocr_path))
            try:
                tree = lxml.etree.parse(self.hocr_path)
            except lxml.etree.XMLSyntaxError as e:
                self.logger.error("Error during parsing of {}"
                                  .format(self.hocr_path))
                self.logger.exception(e)
                raise StopIteration
            for page_node in tree.iterfind(XPATH_PAGE, namespaces=NAMESPACES):
                title_data = self._parse_title(page_node.attrib.get('title'))
                if title_data is None or 'image' not in title_data:
                    self.logger.warn(
                        "Could not parse title data for page with id={} "
                        "on book {}"
                        .format(page_node.attrib.get('id'), self.hocr_path))
                    continue
                img_path = os.path.realpath(
                        os.path.join(os.path.dirname(self.hocr_path),
                                     title_data['image']))
                if 'bbox' not in title_data:
                    dimensions = Image.open(img_path).size
                else:
                    dimensions = [
                        int(x) for x in title_data['bbox'].split()[2:]]
                page_id = page_node.attrib['id']
                self._images[page_id] = (dimensions, img_path)
                yield page_id, dimensions, img_path

    def get_lines(self, page_id):
        if self._lines:
            for bbox, text in self._lines[page_id]:
                yield bbox, text
        else:
            self.logger.debug("Cache miss while getting lines for {}"
                              .format(self.hocr_path))
            tree = lxml.etree.parse(self.hocr_path)
            for page_node in tree.iterfind(XPATH_PAGE, namespaces=NAMESPACES):
                page_id = page_node.attrib.get('id')
                self._lines[page_id] = []
                line_nodes = page_node.iterfind(XPATH_LINE,
                                                namespaces=NAMESPACES)
                for line_node in line_nodes:
                    title_data = self._parse_title(
                        line_node.attrib.get('title'))
                    if title_data is None:
                        self.logger.warn(
                            "Could not parse title data for line with id={} "
                            "on page with id={} on book {}"
                            .format(line_node.attrib.get('id'), page_id,
                                    self.hocr_path))
                        continue
                    bbox = [int(v) for v in title_data['bbox'].split()]
                    text = "".join(line_node.itertext())
                    self._lines[page_id].append((bbox, text))
                    yield bbox, text


def locate_image(uid):
    book_id, page_id = uid.split(':')
    book = repository.get(book_id)
    return book.get_image_path(page_id)


class HocrViewerApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, base_dir, base_url):
        self.options = {'bind': '0.0.0.0:5000',
                        'workers': cpu_count()*2+1}
        self.application = app
        app.config['IIIF_CACHE_HANDLER'] = ImageSimpleCache()
        app.config['BASE_DIR'] = base_dir
        app.config['BASE_URL'] = base_url
        ext.uuid_to_image_opener_handler(locate_image)
        super(HocrViewerApplication, self).__init__()

    def load_config(self):
        config = dict([(key, value) for key, value in self.options.items()
                       if key in self.cfg.settings and value is not None])
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.application


def build_manifest(book):
    images = book.get_images()
    fac = ManifestFactory()
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    fac.set_base_metadata_uri(
        base_url + flask.url_for('get_book_manifest', book_id=book.id))
    fac.set_base_image_uri(base_url + '/iiif/image/v2')
    fac.set_iiif_image_info(2.0, 2)
    manifest = fac.manifest(label=book.id)
    manifest.set_description("Automatically generated from HOCR")
    seq = manifest.sequence(ident='0')
    for idx, imginfo in enumerate(images):
        page_id, dimensions, img_path = imginfo
        canvas = seq.canvas(ident=page_id,
                            label='Page {}'.format(idx))
        anno = canvas.annotation(ident=page_id)
        img = anno.image('{}:{}'.format(book.id, page_id), iiif=True)
        img.set_hw(dimensions[1], dimensions[0])
        canvas.height = img.height
        canvas.width = img.width
        canvas.annotationList(
            base_url + flask.url_for('get_page_lines', book_id=book.id,
                                     page_id=page_id),
            label="Transcribed Text")
    if not seq.canvases:
        logger.error("{} has no images!".format(book.hocr_path))
        return None
    else:
        return manifest


def get_canvas_id(book_id, page_id):
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    return (base_url + flask.url_for('get_book_manifest', book_id=book_id) +
            '/canvas/' + page_id + '.json')


@app.route("/iiif/<book_id>")
def get_book_manifest(book_id):
    doc = repository.get(book_id)
    if doc is None:
        flask.abort(404)
    manifest = build_manifest(doc)
    if manifest is None:
        flask.abort(500)
    return flask.jsonify(manifest.toJSON(top=True))


@app.route("/iiif/<book_id>/list/<page_id>", methods=['GET'])
@app.route("/iiif/<book_id>/list/<page_id>.json", methods=['GET'])
def get_page_lines(book_id, page_id):
    book = repository.get(book_id)
    lines = book.get_lines(page_id)

    fac = ManifestFactory()
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    fac.set_base_metadata_uri(base_url + '/iiif/' + book_id)
    annotation_list = fac.annotationList(ident=page_id)
    for idx, line in enumerate(lines):
        bbox, text = line
        ulx, uly, lrx, lry = bbox
        width = lrx - ulx
        height = lry - uly
        anno = annotation_list.annotation(ident='line-{}'.format(idx))
        anno.text(text=text)
        anno.on = (get_canvas_id(book_id, page_id) +
                   "#xywh={},{},{},{}".format(ulx, uly, width, height))
    out_data = annotation_list.toJSON(top=True)
    if not annotation_list.resources:
        # NOTE: iiif-prezi strips empty list from the resulting JSON,
        #       so we have to add the empty list ourselves...
        out_data['resources'] = []
    return flask.jsonify(out_data)


@app.route('/')
def index():
    return flask.render_template('mirador.html',
                                 book_ids=repository.book_ids())


@click.command()
@click_log.simple_verbosity_option()
@click_log.init(__name__)
@click.argument('hocr-directory', type=click.Path(file_okay=False, exists=True,
                                                  readable=True))
@click.option('-u', '--base-url', default='http://127.0.0.1:5000',
              help='HTTP URL where the application is reachable')
def cli(hocr_directory, base_url):
    global repository
    repository = HocrRepository(hocr_directory)
    HocrViewerApplication(app, hocr_directory, base_url).run()


if __name__ == '__main__':
    cli()
