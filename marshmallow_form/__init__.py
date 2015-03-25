# -*- coding:utf-8 -*-
import logging
import copy
from functools import partial
from collections import ChainMap
from marshmallow import fields
from marshmallow.compat import text_type
from marshmallow.exceptions import MarshmallowError, MarshallingError
from .lazylist import LazyList
logger = logging.getLogger(__name__)


class MarshmallowFormError(MarshmallowError):
    pass


class LayoutTooFew(MarshmallowFormError):
    pass


class LayoutTooMany(MarshmallowFormError):
    pass


class reify(object):
    def __init__(self, wrapped):
        self.wrapped = wrapped
        try:
            self.__doc__ = wrapped.__doc__
        except:  # pragma: no cover
            pass

    def __get__(self, inst, objtype=None):
        if inst is None:
            return self
        val = self.wrapped(inst)
        setattr(inst, self.wrapped.__name__, val)
        return val


class Counter(object):
    def __init__(self, i):
        self.i = i

    def __call__(self):
        v = self.i
        self.i += 1
        return v

C = Counter(0)


class RegisterAction(object):
    def __init__(self, action, method):
        self.action = action
        self.method = method

    def register(self, schema):
        self.action(schema, self.method)


class Field(object):
    def __init__(self, field):
        self.field = field
        self.name = None
        self._c = C()

    def expose(self):
        return self.field

    def __get__(self, ob, type_):
        if ob is None:
            return self
        name = self.name
        field = ob.schema.fields[name]
        bf = bound_field(name, field, ob, overrides=ob.metadata.get(name))
        ob.__dict__[name] = bf
        return bf


def bound_field(name, field, ob, key=None, overrides=None):
    if hasattr(field, "nested"):
        return NestedBoundField(name, field, ob, overrides=overrides)
    else:
        return BoundField(name, field, ob, key=key, overrides=overrides)


class BoundField(object):
    def __init__(self, name, field, form, key=None, overrides=None):
        self.name = name
        self.key = key or name
        self.field = field
        self.form = form
        self.overrides = overrides

    def __iter__(self):
        yield self

    @reify
    def metadata(self):
        if self.overrides:
            return ChainMap(self.overrides, self.field.metadata)
        else:
            return self.field.metadata

    def __getitem__(self, k):
        return self.form.itemgetter(self.metadata, k)

    def __getattr__(self, k):
        return getattr(self.field, k)

    def disabled(self):
        self.metadata["disabled"] = True

    @reify
    def choices(self):
        if "pairs" in self.metadata:
            return self.metadata["pairs"]
        elif hasattr(self.field, "labels"):
            labelgetter = self.metadata.get("labelgetter") or text_type
            return LazyList(self.field.labels(labelgetter))
        else:
            return []

    @reify
    def value(self):
        return (self.form.data.get(self.key)
                or self.form.initial.get(self.key)
                or self.field.default)


class SubForm(object):
    def __init__(self, data, initial, itemgetter):
        self.data = data
        self.initial = initial
        self.itemgetter = itemgetter

    @classmethod
    def from_form(cls, name, form):
        data = (form.data.get(name) if form.data else None) or {}
        initial = (form.initial.get(name) if form.initial else None) or {}
        return cls(data, initial, itemgetter=form.itemgetter)


class NestedBoundField(BoundField):
    def __init__(self, name, field, form, overrides=None):
        self._name = name
        self.field = field
        self.form = form
        self.overrides = overrides

    @reify
    def children(self):
        return copy.deepcopy(self.field.nested._declared_fields)

    def __iter__(self):
        for k in self.children.keys():
            for f in getattr(self, k):
                yield f

    @reify
    def metadata(self):
        if self.overrides:
            return ChainMap(self.overrides, self.field.metadata)
        else:
            return self.field.metadata

    def __getitem__(self, k):
        return self.form.itemgetter(self.metadata, k)

    def __getattr__(self, k):
        if k not in self.children:
            raise AttributeError(k)
        subform = SubForm.from_form(self._name, self.form)
        name = "{}.{}".format(self._name, k)
        bf = bound_field(name, self.children[k], subform, key=k, overrides=self.metadata.get(k))
        setattr(self, k, bf)
        return bf


def field(fieldclass, *args, **kwargs):
    return Field(fieldclass(*args, **kwargs))


class FormMeta(type):
    from marshmallow import Schema
    SchemaBase = Schema

    @staticmethod
    def access(self, k, ob):
        return getattr(ob, k)

    def __new__(self, name, bases, attrs):
        # todo: rewrite
        # - collecting schema
        # - make_object
        # - layout

        schema_attrs = {}
        fields = {}
        register_actions = []
        schema_bases = []
        metadata = {}

        for b in bases:
            if hasattr(b, "ordered_names"):
                for k in b.ordered_names:
                    v = getattr(b, k)
                    schema_attrs[k] = v.expose()
                    fields[k] = v
            if hasattr(b, "metadata"):
                metadata.update(b.metadata)
            if hasattr(b, "Schema") and issubclass(b.Schema, self.Schema):
                if b.Schema not in schema_bases:
                    schema_bases.append(b.Schema)
        if len(schema_bases) <= 0:
            schema_bases.append(self.SchemaBase)

        for k, v in attrs.items():
            if hasattr(v, "expose"):
                v.name = k
                schema_attrs[k] = v.expose()
                fields[k] = v
            if hasattr(v, "register") and callable(v.register):
                register_actions.append(v)

        layout = None
        if "Meta" in attrs:
            meta = attrs["Meta"]
            layout = getattr(meta, "layout", None)
            metadata.update(getattr(meta, "metadata", {}))
            metadata.update(getattr(meta, "overrides", {}))
            if hasattr(meta, "itemgetter"):
                if isinstance(meta.itemgetter, staticmethod):
                    attrs["itemgetter"] = meta.itemgetter
                else:
                    attrs["itemgetter"] = staticmethod(meta.itemgetter)

        # this is meta of marshmallow Schema
        class Meta:
            ordered = True
        schema_attrs["Meta"] = Meta

        if "make_object" in attrs:
            schema_attrs["make_object"] = attrs.pop("make_object")

        attrs["ordered_names"] = [f.name for f in sorted(fields.values(), key=lambda f: f._c)]
        attrs["register_actions"] = register_actions
        schema_class = self.SchemaBase.__class__(
            name.replace("Form", "Schema"),
            tuple(schema_bases),
            schema_attrs)
        # schema_class.accessor(self.access)
        attrs["Schema"] = schema_class

        cls = super().__new__(self, name, bases, attrs)

        if layout is not None:
            layout.check_shape(cls())
        cls.layout = layout or FlattenLayout()

        cls.metadata = metadata

        for ac in register_actions:
            ac.register(cls.Schema)
        return cls


class Layout(object):
    def __init__(self, shape):
        self.shape = shape

    def set_from_shape(self, shape, s):
        if isinstance(shape, (tuple, list, LColumn)):
            for row in shape:
                self.set_from_shape(row, s)
        else:
            s.add(shape)

    def check_shape(self, form):
        actual_set = set()
        self.set_from_shape(self.shape, actual_set)
        expected_set = set(bf.name for bf in form)
        diff = expected_set.difference(actual_set)
        if diff:
            raise LayoutTooFew(diff)
        diff = actual_set.difference(expected_set)
        if diff:
            raise LayoutTooMany(diff)

    def build_iterator(self, form, shape):
        if isinstance(shape, (list, tuple)):
            return [self.build_iterator(form, row) for row in shape]
        elif isinstance(shape, LColumn):
            fields = [self.build_iterator(form, row) for row in shape]
            return (shape, fields)
        else:
            target = form
            for k in shape.split("."):
                target = getattr(target, k)
            return target

    def __call__(self, form):
        return iter(self.build_iterator(form, self.shape))


class LColumn(object):
    def __init__(self, *fields, **metadata):
        self.fields = fields
        self.metadata = metadata

    def __getitem__(self, k):
        return self.metadata[k]

    def __iter__(self):
        return iter(self.fields)


class FlattenLayout(object):
    def __call__(self, form):
        for name in form.ordered_names:
            for bfield in getattr(form, name):
                yield bfield


class FormBase(object):
    itemgetter = staticmethod(lambda d, k: d.get(k, ""))
    error_handler = partial(RegisterAction, (lambda schema, method: schema.error_handler(method)))
    data_handler = partial(RegisterAction, (lambda schema, method: schema.data_handler(method)))
    validator = partial(RegisterAction, (lambda schema, method: schema.validator(method)))
    preprocessor = partial(RegisterAction, (lambda schema, method: schema.preprocessor(method)))
    accessor = partial(RegisterAction, (lambda schema, method: schema.accessor(method)))

    def __init__(self, data=None, initial=None, prefix="", options={"strict": False}, metadata=None):
        self.options = options
        self.rawdata = data or {}
        self.data = self.rawdata.copy()
        self.initial = initial or {}
        self.errors = None
        self.prefix = prefix
        self.metadata = copy.deepcopy(self.metadata)
        if metadata:
            self.metadata.update(metadata)

    @reify
    def schema(self):
        return self.Schema(**self.options)

    @classmethod
    def from_object(cls, ob, *args, **kwargs):
        form = cls(*args, **kwargs)
        data = form.serialize(ob)
        form.rawdata = data
        form.data = data.copy()
        return form

    def add_field(self, name, field):
        if hasattr(field, "expose"):
            field = field.expose()
        self.schema.fields[name] = field
        setattr(self, name, BoundField(name, field, self))

    def remove_field(self, name):
        if hasattr(self, name):
            delattr(self, name)
            del self.schema.fields[name]
        if "ordered_names" not in self.__dict__:
            self.ordered_names = self.ordered_names[:]
        self.ordered_names.remove(name)

    def __iter__(self):
        return iter(self.layout(self))

    def _parsing_iterator(self, name, field):
        if hasattr(field, "nested"):
            for subname, f in field.nested._declared_fields.items():
                for subname, subf in self._parsing_iterator(subname, f):
                    yield "{}.{}".format(name, subname), subf
        else:
            yield name, field

    def cleansing(self, data=None):
        data = data or self.data
        result = d = {}
        for name, f in self.schema.fields.items():
            for k, f in self._parsing_iterator(name, f):
                d = result
                v = data.get(self.prefix + k, "")
                if v == "" and (f.required or not isinstance(f, fields.String)):
                    continue
                ts = k.split(".")
                for t in ts[:-1]:
                    if t not in d:
                        d[t] = {}
                    d = d[t]
                d[ts[-1]] = v
        return result

    def has_errors(self):
        return bool(self.errors)

    def validate(self, data=None, cleansing=True):
        self.deserialize(data=data, cleansing=True)
        return not self.has_errors()

    def load(self, data=None, cleansing=True):
        data = data or self.data
        if cleansing:
            data = self.cleansing(data)
        return self.schema.load(data)

    def deserialize(self, data=None, cleansing=True):
        result = self.load(data=data, cleansing=cleansing)
        self.errors = result.errors
        self.data = result.data
        return result.data

    def dump(self, data=None):
        data = data or self.data
        return self.schema.dump(data)

    def serialize(self, data=None):
        result = self.dump(data=data)
        if result.errors:
            raise MarshallingError(result.errors)
        return result.data

    def __getitem__(self, k):
        return self.itemgetter(self.metadata, k)

Form = FormMeta("Form", (FormBase, ), {})


# TODO:
class ModelForm(Form):
    def __init__(self, *args, **kwargs):
        self.model = kwargs.pop("model", None)
        super(ModelForm, self).__init__(*args, **kwargs)


def select_wrap(pairs, *args, **kwargs):
    choices = [p[0] for p in pairs]
    kwargs["pairs"] = pairs
    return fields.Select(choices, *args, **kwargs)


def nested_wrap(formclass, *args, **kwargs):
    schema = formclass.Schema
    kwargs.update(kwargs.pop("overrides", {}))
    return fields.Nested(schema, *args, **kwargs)


if __name__ != "__main__":
    Nested = partial(field, nested_wrap, required=True)

    Price = partial(field, fields.Price, required=True)
    Arbitrary = partial(field, fields.Arbitrary, required=True)
    Decimal = partial(field, fields.Decimal, required=True)
    DateTime = partial(field, fields.DateTime, required=True)
    URL = partial(field, fields.URL, required=True)
    Time = partial(field, fields.Time, required=True)
    Str = partial(field, fields.Str, required=True)
    Bool = partial(field, fields.Bool, required=True)
    String = partial(field, fields.String, required=True)
    Url = partial(field, fields.Url, required=True)
    LocalDateTime = partial(field, fields.LocalDateTime, required=True)
    Float = partial(field, fields.Float, required=True)
    Email = partial(field, fields.Email, required=True)
    Date = partial(field, fields.Date, required=True)
    Int = partial(field, fields.Int, required=True)
    TimeDelta = partial(field, fields.TimeDelta, required=True)
    UUID = partial(field, fields.UUID, required=True)
    Function = partial(field, fields.Function, required=True)
    FormattedString = partial(field, fields.FormattedString, required=True)
    Number = partial(field, fields.Number, required=True)
    Method = partial(field, fields.Method, required=True)
    Raw = partial(field, fields.Raw, required=True)
    Select = partial(field, select_wrap, required=True)
    Fixed = partial(field, fields.Fixed, required=True)
    QuerySelect = partial(field, fields.QuerySelect, required=True)
    ValidatedField = partial(field, fields.ValidatedField, required=True)
    Integer = partial(field, fields.Integer, required=True)
    QuerySelectList = partial(field, fields.QuerySelectList, required=True)
    Boolean = partial(field, fields.Boolean, required=True)
    List = partial(field, fields.List, required=True)

# from prestring.python import PythonModule
# m = PythonModule()
# for k, v in fields.__dict__.items():
#     if isinstance(v, type) and issubclass(v, fields.):
#         m.stmt("{} = partial(field, fields.{})".format(k, k))
# print(m)
