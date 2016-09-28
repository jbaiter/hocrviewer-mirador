from __future__ import print_function

import os
import re
import sys
from collections import OrderedDict
from multiprocessing import cpu_count

import flask
import gunicorn.app.base
import lxml.etree
from flask_iiif import IIIF
from flask_iiif.cache.simple import ImageSimpleCache
from flask_restful import Api
from iiif_prezi.factory import ManifestFactory
from PIL import Image

NAMESPACES = {'xhtml': 'http://www.w3.org/1999/xhtml'}


app = flask.Flask('hocrviewer', static_folder='vendor/mirador/build/mirador',
                  static_url_path='/static')
ext = IIIF(app=app)
api = Api(app=app)
ext.init_restful(api, prefix="/iiif/image/")
book_cache = {}
image_locator = None


class HocrDocument(object):
    def __init__(self, book_id, hocr_path):
        self.id = book_id
        self.hocr_path = hocr_path
        self._images = OrderedDict()
        self._lines = OrderedDict()

    def _parse_title(self, title):
        if title is None:
            return
        return {itm.split(" ")[0]: " ".join(itm.split(" ")[1:])
                for itm in title.split("; ")}

    def get_images(self):
        if self._images:
            for page_id, data in self._images.items():
                dimensions, fpath = data
                yield page_id, dimensions, fpath
        else:
            tree = lxml.etree.parse(self.hocr_path)
            for page_node in tree.findall(".//xhtml:div[@class='ocr_page']",
                                          namespaces=NAMESPACES):
                title_data = self._parse_title(page_node.attrib.get('title'))
                if title_data is None or 'image' not in title_data:
                    continue
                img_path = os.path.join(os.path.dirname(self.hocr_path),
                                        title_data['image'])
                if 'bbox' not in title_data:
                    dimensions = Image.open(img_path).size
                else:
                    dimensions = [
                        int(x) for x in title_data['bbox'].split()[2:]]
                page_id = page_node.attrib['id'].split('_')[1]
                self._images[page_id] = (dimensions, img_path)
                yield page_id, dimensions, img_path

    def get_lines(self, page_id):
        if page_id in self._lines:
            for bbox, text in self._lines[page_id]:
                yield bbox, text
        else:
            tree = lxml.etree.parse(self.hocr_path)
            self._lines[page_id] = []
            xpath = (".//xhtml:div[@class='ocr_page'][@id='page_{:04}']/"
                     "xhtml:span[@class='ocr_line']".format(int(page_id)))
            line_nodes = tree.findall(xpath, namespaces=NAMESPACES)
            for line_node in line_nodes:
                title_data = self._parse_title(line_node.attrib.get('title'))
                if title_data is None:
                    continue
                bbox = [int(v) for v in title_data['bbox'].split()]
                text = "".join(line_node.itertext())
                self._lines[page_id].append((bbox, text))
                yield bbox, text


class ImageLocator(object):
    def __init__(self, base_directory=None):
        self.base_dir = base_directory

    def __call__(self, uid):
        if self.base_dir is None:
            raise ValueError("Please set base_dir first!")
        image_dir, pageno = re.findall(r'^(.*)_(\d+)$', uid)[0]
        pageno = int(pageno) + 1
        image_dir = os.path.join(self.base_dir, image_dir)
        for fname in os.listdir(image_dir):
            stem, ext = os.path.splitext(fname)
            if stem.isdigit() and int(stem) == pageno and ext != '.tif':
                return os.path.join(image_dir, fname)


class HocrViewerApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, base_dir, base_url):
        self.options = {'bind': '0.0.0.0:5000',
                        'workers': cpu_count()*2+1}
        self.application = app
        app.config['IIIF_CACHE_HANDLER'] = ImageSimpleCache()
        app.config['BASE_DIR'] = base_dir
        app.config['BASE_URL'] = base_url
        image_locator = ImageLocator(base_dir)
        ext.uuid_to_image_opener_handler(image_locator)
        self.init_cache()
        super(HocrViewerApplication, self).__init__()

    def load_config(self):
        config = dict([(key, value) for key, value in self.options.items()
                       if key in self.cfg.settings and value is not None])
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def init_cache(self):
        print("Initializing book cache")
        book_ids = [p[:-5] for p in os.listdir(app.config['BASE_DIR'])
                    if p.endswith('.html')]
        for idx, book_id in enumerate(book_ids):
            sys.stdout.write("{}/{}\r".format(idx, len(book_ids)))
            sys.stdout.flush()
            try:
                get_document(book_id)
            except:
                print("Could not parse {}".format(book_id))


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
        img = anno.image('{}_{}'.format(book.id, idx), iiif=True)
        img.set_hw(dimensions[1], dimensions[0])
        canvas.height = img.height
        canvas.width = img.width
        canvas.annotationList(
            base_url + flask.url_for('get_page_lines', book_id=book.id,
                                     page_id=page_id),
            label="Transcribed Text")
    return manifest


def get_canvas_id(book_id, page_id):
    base_url = app.config.get('BASE_URL', 'http://localhost:5000')
    return (base_url + flask.url_for('get_book_manifest', book_id=book_id) +
            '/canvas/' + page_id + '.json')


def get_document(book_id):
    if book_id in book_cache:
        book = book_cache[book_id]
    else:
        hocr_path = os.path.join(app.config['BASE_DIR'], book_id + '.html')
        if not os.path.exists(hocr_path):
            return None
        book = HocrDocument(book_id, hocr_path)
        book_cache[book_id] = book
    return book


@app.route("/iiif/<book_id>")
def get_book_manifest(book_id):
    doc = get_document(book_id)
    if doc is None:
        flask.abort(404)
    return flask.jsonify(build_manifest(doc).toJSON(top=True))


@app.route("/iiif/<book_id>/list/<page_id>", methods=['GET'])
@app.route("/iiif/<book_id>/list/<page_id>.json", methods=['GET'])
def get_page_lines(book_id, page_id):
    book = get_document(book_id)
    lines = book.get_lines(page_id)

    fac = ManifestFactory()
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
    #if not annotation_list.resources:
        # NOTE: iiif-prezi strips empty list from the resulting JSON,
        #       so we have to add the empty list ourselves...
    #    out_data['resources'] = []
    return flask.jsonify(out_data)


@app.route('/')
def index():
    return flask.render_template('mirador.html', book_ids=book_cache.keys())


if __name__ == '__main__':
    base_dir = sys.argv[1]
    if len(sys.argv) > 2:
        base_url = sys.argv[2]
    else:
        base_url = 'http://localhost:5000'
    HocrViewerApplication(app, base_dir, base_url).run()
