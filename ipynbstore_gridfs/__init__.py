"""An IPython notebook manager with GridFS as the backend."""

from tornado import web
import pymongo
import gridfs
import datetime
import json
from IPython.html.services.contents.manager import ContentsManager
from IPython.utils import tz
from IPython import nbformat
from IPython.utils.traitlets import Unicode


class GridFSContentsManager(ContentsManager):

    mongo_uri = Unicode(
        'mongodb://localhost:27017/',
        config=True,
        help="The URI to connect to the MongoDB instance. \
        Defaults to 'mongodb://localhost:27017/'")

    notebook_collection = Unicode(
        'ipynb',
        config=True,
        help="Collection in mongo where notebook files are stored")

    checkpoint_collection = Unicode(
        'ipynb_checkpoints',
        config=True,
        help="Collection in mongo where notebook files are stored")

    checkpoints_history = Unicode(
        'ipynb_cphistory',
        config=True,
        help="Collection for checkpoints history")

    def __init__(self, **kwargs):
        super(GridFSContentsManager, self).__init__(**kwargs)
        self.MONGODB_DETAILS = pymongo.uri_parser.parse_uri(self.mongo_uri)
        self.mongo_username = self.MONGODB_DETAILS['username'] or ''
        self.mongo_password = self.MONGODB_DETAILS['password'] or ''
        #if db is not specified
        self.database_name = self.MONGODB_DETAILS['database'] or 'ipython'
        self._conn = self._connect_server()

    #Mongo connectors
    def _connect_server(self):
        """
        Returns a mongo client instance
        """
        return pymongo.MongoClient(self.mongo_uri)

    def _connect_collection(self, collection):
        """
        Returns the collection
        """
        db = self._conn[self.database_name]

        # Authenticate against database
        if self.mongo_username and self.mongo_password:
            db.authenticate(self.mongo_username, self.mongo_password)
        return db[collection]

    def _get_fs_instance(self):
        """
        Returns a GridFS instance
        """
        try:
            db = getattr(pymongo.MongoClient(
                self.mongo_uri), self.database_name)
            fs = gridfs.GridFS(db, collection=self.notebook_collection)
        except Exception, e:
            raise e
        return fs

    def file_exists(self, path):
        """
        Does a file exist at the given collection in gridFS?
        Like os.path.exists
        Parameters
        ----------
        path : string
            The name of the file in the gridfs
        Returns
        -------
        exists : bool
            Whether the target exists.
        """
        path = path.strip('/')
        file_collection = self._get_fs_instance().list()
        if path == '':
            return False
        if path in file_collection:
            return True
        return False

    def exists(self, path):
        """
        Does a file or dir exist at the given collection in gridFS?
        We do not have dir so dir_exists returns true.
        Parameters
        ----------
        path : string
            The relative path to the file's directory (with '/' as separator)
        Returns
        -------
        exists : bool
            Whether the target exists.
        """
        path = path.strip('/')
        return self.file_exists(path) or self.dir_exists(path)

    def dir_exists(self, path=''):
        """
        GridFS doesn't have directory so it doesn't exist
        If path is set to blank, returns True because,
        technically, the collection root itself is a directory
        """
        if path == '':
            return True
        else:
            return False

    def is_hidden(self, path=''):
        """
        gridfs doesn't hide anything
        """
        return False

    def get(self, path, content=True, type=None, format=None):
        """
        Takes a filename for an entity and returns its model
        Parameters
        ----------
        path : str
            the filename that is expected to be in gridfs
        content : bool
            Whether to include the contents in the reply
        type : str, optional
            The requested type - 'notebook'
            Will raise HTTPError 400 if the content doesn't match.
        format : str, optional
            The requested format for file contents. 'text' or 'base64'.
            Ignored if this returns a notebook or directory model.
        Returns
        -------
        model : dict
            the contents model. If content=True, returns the contents
            of the file or directory as well.
        """
        path = path.strip('/')
        if not self.exists(path):
            raise web.HTTPError(404, u'No such file or directorys: %s' % path)

        if path == '':
            if type not in (None, 'directory'):
                raise web.HTTPError(400, u'%s is a directory, not a %s' % (
                    path, type), reason='bad type')
            model = self._dir_model(path, content=content)
        elif type == 'notebook' or (type is None and path.endswith('.ipynb')):
            model = self._notebook_model(path, content=content)
        else:
            raise web.HTTPError(400, u'%s is not a directory' % path,
                                reason='bad type')
            model = self._file_model(path, content=content, format=format)

        return model

    # public checkpoint API
    def create_checkpoint(self, path=''):
        """
        Creates a checkpoint for the notebook
        """
        path = path.strip('/')
        spec = {
            'path': path,
        }

        notebook = self._get_fs_instance().get_version(path)
        chid = str(notebook._id)
        cp_id = str(self._connect_collection(
            self.checkpoint_collection).find(spec).count())

        spec['cp'] = cp_id
        spec['id'] = chid
        last_modified = datetime.datetime.utcnow()
        spec['lastModified'] = last_modified

        newnotebook = {'$set': {'_id': chid}}

        self.log.info("Saving checkpoint for notebook %s" % path)
        self._connect_collection(
            self.checkpoint_collection).update(spec, newnotebook, upsert=True)

        # return the checkpoint info
        return dict(id=cp_id, last_modified=last_modified)

    def list_checkpoints(self, path=''):
        """
        lists all checkpoints for the notebook
        """
        path = path.strip('/')
        spec = {
            'path': path,
        }
        checkpoints = list(self._connect_collection(
            self.checkpoint_collection).find(spec))
        return [dict(
            id=c['cp'], last_modified=c['lastModified']) for c in checkpoints]

    def save(self, model, path=''):
        """
        Save the file model and return the model with no content.
        """
        path = path.strip('/')

        if 'type' not in model:
            raise web.HTTPError(400, u'No file type provided')
        if 'content' not in model and model['type'] != 'directory':
            raise web.HTTPError(400, u'No file content provided')

        # One checkpoint should always exist
        if self.file_exists(path) and not self.list_checkpoints(path):
            self.create_checkpoint(path)

        self.log.debug("Saving %s", path)

        self.run_pre_save_hook(model=model, path=path)

        try:
            if model['type'] == 'notebook':
                nb = nbformat.from_dict(model['content'])
                self.check_and_sign(nb, path)
                self._save_notebook(path, nb)
                # One checkpoint should always exist for notebooks.
                # if not self.list_checkpoints(path):
                #     self.create_checkpoint(path)
            elif model['type'] == 'file':
                # Missing format will be handled internally by _save_file.
                self._save_file(path, model['content'], model.get('format'))
            elif model['type'] == 'directory':
                self._save_directory(path, model, path)
            else:
                raise web.HTTPError(
                    400, "Unhandled contents type: %s" % model['type'])
        except web.HTTPError:
            raise
        except Exception as e:
            self.log.error(
                u'Error while saving file: %s %s', path, e, exc_info=True)
            raise web.HTTPError(
                500, u'Unexpected error while saving file: %s %s' % (path, e))

        validation_message = None
        if model['type'] == 'notebook':
            self.validate_notebook_model(model)
            validation_message = model.get('message', None)

        model = self.get(path, content=False)
        if validation_message:
            model['message'] = validation_message

        return model

    def rename(self, old_path, new_path):
        """
        Renames a notebook
        """
        old_path = old_path.strip('/')
        new_path = new_path.strip('/')
        fs = self._get_fs_instance()
        if new_path == old_path:
            return

        if self.file_exists(new_path):
            raise web.HTTPError(409, u'Notebook already exists: %s' % new_path)

        # Move the file
        try:
            grid_file = fs.get_version(old_path)._id
            nb = nbformat.from_dict(
                json.loads(fs.get(grid_file).read()))
            fs.put(json.dumps(nb), filename=new_path)
            self.delete(old_path)
        except Exception as e:
            raise web.HTTPError(500, u'Unknown error renaming file: %s %s' % (
                old_path, e))

        # Move the checkpoints
        spec = {
            'path': old_path,
        }
        modify = {
            '$set': {
                'path': new_path,
            }
        }
        self._connect_collection(
            self.checkpoint_collection).update(spec, modify, multi=True)

    def delete(self, path):
        """Delete notebook"""
        if not self.file_exists(path):
            print "hello"
            return
        path = path.strip('/')
        fs = self._get_fs_instance()
        gridfile = fs.get_version(path)._id
        fs.delete(gridfile)


    def _save_notebook(self, os_path, nb):
        """Saves a notebook to an gridFS."""
        self._get_fs_instance().put(
            json.dumps(nb, sort_keys=True), filename=os_path)

    def _base_model(self, path):
        """Build the common base of a contents model"""
        last_modified = tz.utcnow()
        created = tz.utcnow()
        # Create the base model.
        model = {}
        model['name'] = path.rsplit('/', 1)[-1]
        model['path'] = path
        model['last_modified'] = last_modified
        model['created'] = created
        model['content'] = None
        model['format'] = None
        model['mimetype'] = None
        model['writable'] = True
        return model

    def _dir_model(self, path, content=True):
        """
        Build a model to return all of the files in gridfs
        if content is requested, will include a listing of the directory
        """
        model = self._base_model(path)
        model['type'] = 'directory'
        model['content'] = contents = []
        file_collection = self._get_fs_instance().list()
        for name in file_collection:
            contents.append(self.get(
                path='%s' % (name),
                content=content)
            )
        model['format'] = 'json'
        return model

    def _notebook_model(self, path, content=True):
        """
        Build a notebook model
        if content is requested, the notebook content will be populated
        as a JSON structure (not double-serialized)
        """
        model = self._base_model(path)
        model['type'] = 'notebook'
        if content:
            nb = self._read_notebook(path, as_version=4)
            self.mark_trusted_cells(nb, path)
            model['content'] = nb
            model['format'] = 'json'
            self.validate_notebook_model(model)
        return model

    def _read_notebook(self, path, as_version=4):
        """Read a notebook file from gridfs."""
        fs = self._get_fs_instance()
        file_id = fs.get_last_version(path)._id
        try:
            filename = fs.get(file_id)
        except Exception, e:
            raise web.HTTPError(
                400,
                u"An error occured while reading the Notebook \
                on GridFS: %s %r" % (path, e),
            )
        try:
            return nbformat.read(filename, as_version=as_version)
        except Exception as e:
            raise web.HTTPError(
                400,
                u"Unreadable Notebook: %s %r" % (path, e),
            )
