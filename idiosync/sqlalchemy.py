"""SQLAlchemy user database"""

from abc import abstractmethod, ABCMeta
import uuid
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative.api import DeclarativeMeta
from .base import Attribute, Entry, User, Group, Config, WritableDatabase

NAMESPACE_SQL = uuid.UUID('b3c23456-05d8-4be5-b173-b57aeb30b4f4')


class SqlAttribute(Attribute):
    """A SQL user database attribute"""
    # pylint: disable=too-few-public-methods

    def __get__(self, instance, owner):
        """Get attribute value"""
        if instance is None:
            return self
        return getattr(instance.row, self.name)

    def __set__(self, instance, value):
        """Set attribute value"""
        setattr(instance.row, self.name, value)


class SqlEntryMeta(ABCMeta):
    """SQL user database entry metaclass"""

    def __init__(cls, name, bases, dct):
        super(SqlEntryMeta, cls).__init__(name, bases, dct)
        # Construct a namespace based on the table name if applicable
        if isinstance(cls.model, DeclarativeMeta):
            cls.uuid_ns = uuid.uuid5(NAMESPACE_SQL, cls.model.__table__.name)


class SqlEntry(Entry, metaclass=SqlEntryMeta):
    """A SQL user database entry"""

    uuid_ns = None
    """UUID namespace for entries within this table"""

    def __init__(self, key):
        super(SqlEntry, self).__init__(key)
        if isinstance(type(self.key), DeclarativeMeta):

            # Key is a prefetched SQLAlchemy row
            self.row = self.key
            self.key = getattr(self.row, self.column)

        else:

            # Key is a column value
            query = self.db.query(self.model).filter(
                getattr(self.model, self.column) == self.key
            )
            self.row = query.one()

    @property
    @abstractmethod
    def model(self):
        """ORM model class"""
        pass

    @property
    @abstractmethod
    def column(self):
        """Key column"""
        pass

    @property
    def uuid(self):
        """Permanent identifier for this entry"""
        identity = inspect(self.row).identity
        return uuid.uuid5(self.uuid_ns, ':'.join(str(x) for x in identity))


class SqlUser(SqlEntry, User):
    """A SQL user database user"""
    # pylint: disable=abstract-method
    pass


class SqlGroup(SqlEntry, Group):
    """A SQL user database group"""
    # pylint: disable=abstract-method
    pass


class SqlConfig(Config):
    """SQL user database configuration"""
    # pylint: disable=too-few-public-methods

    def __init__(self, uri, **kwargs):
        super(SqlConfig, self).__init__(**kwargs)
        self.uri = uri


class SqlDatabase(WritableDatabase):
    """A SQL user database"""

    Config = SqlConfig
    User = SqlUser
    Group = SqlGroup

    def __init__(self, **kwargs):
        super(SqlDatabase, self).__init__(**kwargs)
        self.engine = create_engine(self.config.uri, **self.config.options)
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.config.uri)

    def query(self, *args, **kwargs):
        """Query database"""
        return self.session.query(*args, **kwargs)

    @property
    def users(self):
        """All users"""
        return (self.user(x) for x in self.query(self.User.model))

    @property
    def groups(self):
        """All groups"""
        return (self.group(x) for x in self.query(self.Group.model))

    def commit(self):
        """Commit database changes"""
        self.session.commit()
