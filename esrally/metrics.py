# Licensed to Elasticsearch B.V. under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Elasticsearch B.V. licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import collections
import logging
import math
import os
import pickle
import random
import statistics
import sys
import time
import zlib
from enum import Enum, IntEnum
from http.client import responses

import tabulate

from esrally import time, exceptions, config, version, paths
from esrally.utils import convert, console, io, versions


class EsClient:
    """
    Provides a stripped-down client interface that is easier to exchange for testing
    """

    def __init__(self, client, cluster_version=None):
        self._client = client
        self.logger = logging.getLogger(__name__)
        self._cluster_version = cluster_version

    # TODO #653: Remove version-specific support for metrics stores before 7.0.0.
    def probe_version(self):
        info = self.guarded(self._client.info)
        try:
            self._cluster_version = versions.components(info["version"]["number"])
        except BaseException:
            msg = "Could not determine version of metrics cluster"
            self.logger.exception(msg)
            raise exceptions.RallyError(msg)

    def put_template(self, name, template):
        # TODO #653: Remove version-specific support for metrics stores before 7.0.0 (also adjust template)
        if self._cluster_version[0] > 6:
            return self.guarded(self._client.indices.put_template, name=name, body=template, params={
                # allows to include the type name although it is not allowed anymore by default
                "include_type_name": "true"
            })
        else:
            return self.guarded(self._client.indices.put_template, name=name, body=template)

    def template_exists(self, name):
        return self.guarded(self._client.indices.exists_template, name)

    def delete_template(self,  name):
        self.guarded(self._client.indices.delete_template, name)

    def get_index(self, name):
        return self.guarded(self._client.indices.get,  name)

    def create_index(self, index):
        # ignore 400 cause by IndexAlreadyExistsException when creating an index
        return self.guarded(self._client.indices.create, index=index, ignore=400)

    def exists(self, index):
        return self.guarded(self._client.indices.exists, index=index)

    def refresh(self, index):
        return self.guarded(self._client.indices.refresh, index=index)

    def bulk_index(self, index, doc_type, items):
        # TODO #653: Remove version-specific support for metrics stores before 7.0.0.
        import elasticsearch.helpers
        if self._cluster_version[0] > 6:
            self.guarded(elasticsearch.helpers.bulk, self._client, items, index=index)
        else:
            self.guarded(elasticsearch.helpers.bulk, self._client, items, index=index, doc_type=doc_type)

    def index(self, index, doc_type, item):
        self.bulk_index(index, doc_type, [{"_source": item}])

    def search(self, index, body):
        return self.guarded(self._client.search, index=index, body=body)

    def guarded(self, target, *args, **kwargs):
        import elasticsearch
        max_execution_count = 11
        execution_count = 0

        while execution_count < max_execution_count:
            time_to_sleep = 2 ** execution_count + random.random()
            execution_count += 1

            try:
                return target(*args, **kwargs)
            except elasticsearch.exceptions.AuthenticationException:
                # we know that it is just one host (see EsClientFactory)
                node = self._client.transport.hosts[0]
                msg = "The configured user could not authenticate against your Elasticsearch metrics store running on host [%s] at " \
                      "port [%s] (wrong password?). Please fix the configuration in [%s]." % \
                      (node["host"], node["port"], config.ConfigFile().location)
                self.logger.exception(msg)
                raise exceptions.SystemSetupError(msg)
            except elasticsearch.exceptions.AuthorizationException:
                node = self._client.transport.hosts[0]
                msg = "The configured user does not have enough privileges to run the operation [%s] against your Elasticsearch metrics " \
                      "store running on host [%s] at port [%s]. Please adjust your x-pack configuration or specify a user with enough " \
                      "privileges in the configuration in [%s]." % \
                      (target.__name__, node["host"], node["port"], config.ConfigFile().location)
                self.logger.exception(msg)
                raise exceptions.SystemSetupError(msg)
            except elasticsearch.exceptions.ConnectionTimeout:
                if execution_count < max_execution_count:
                    self.logger.debug("Connection timeout in attempt [%d/%d].", execution_count, max_execution_count)
                    time.sleep(time_to_sleep)
                else:
                    operation = target.__name__
                    self.logger.exception("Connection timeout while running [%s] (retried %d times).", operation, max_execution_count)
                    node = self._client.transport.hosts[0]
                    msg = "A connection timeout occurred while running the operation [%s] against your Elasticsearch metrics store on " \
                          "host [%s] at port [%s]." % (operation, node["host"], node["port"])
                    raise exceptions.RallyError(msg)
            except elasticsearch.exceptions.ConnectionError:
                node = self._client.transport.hosts[0]
                msg = "Could not connect to your Elasticsearch metrics store. Please check that it is running on host [%s] at port [%s]" \
                      " or fix the configuration in [%s]." % (node["host"], node["port"], config.ConfigFile().location)
                self.logger.exception(msg)
                raise exceptions.SystemSetupError(msg)
            except elasticsearch.TransportError as e:
                if e.status_code in (502, 503, 504, 429) and execution_count < max_execution_count:
                    self.logger.debug("%s (code: %d) in attempt [%d/%d]. Sleeping for [%f] seconds.",
                                      responses[e.status_code], e.status_code, execution_count, max_execution_count, time_to_sleep)
                    time.sleep(time_to_sleep)
                else:
                    node = self._client.transport.hosts[0]
                    msg = "A transport error occurred while running the operation [%s] against your Elasticsearch metrics store on " \
                          "host [%s] at port [%s]." % (target.__name__, node["host"], node["port"])
                    self.logger.exception(msg)
                    raise exceptions.RallyError(msg)

            except elasticsearch.exceptions.ElasticsearchException:
                node = self._client.transport.hosts[0]
                msg = "An unknown error occurred while running the operation [%s] against your Elasticsearch metrics store on host [%s] " \
                      "at port [%s]." % (target.__name__, node["host"], node["port"])
                self.logger.exception(msg)
                # this does not necessarily mean it's a system setup problem...
                raise exceptions.RallyError(msg)


class EsClientFactory:
    """
    Abstracts how the Elasticsearch client is created. Intended for testing.
    """

    def __init__(self, cfg):
        self._config = cfg
        host = self._config.opts("reporting", "datastore.host")
        port = self._config.opts("reporting", "datastore.port")
        secure = convert.to_bool(self._config.opts("reporting", "datastore.secure"))
        user = self._config.opts("reporting", "datastore.user")
        password = self._config.opts("reporting", "datastore.password")
        verify = self._config.opts("reporting", "datastore.ssl.verification_mode", default_value="full", mandatory=False) != "none"
        ca_path = self._config.opts("reporting", "datastore.ssl.certificate_authorities", default_value=None, mandatory=False)
        self.probe_version = self._config.opts("reporting", "datastore.probe.cluster_version", default_value=True, mandatory=False)

        from esrally import client

        # Instead of duplicating code, we're just adapting the metrics store specific properties to match the regular client options.
        client_options = {
            "use_ssl": secure,
            "verify_certs": verify,
            "timeout": 120
        }
        if ca_path:
            client_options["ca_certs"] = ca_path
        if user and password:
            client_options["basic_auth_user"] = user
            client_options["basic_auth_password"] = password

        factory = client.EsClientFactory(hosts=[{"host": host, "port": port}], client_options=client_options)
        self._client = factory.create()

    def create(self):
        c = EsClient(self._client)
        if self.probe_version:
            c.probe_version()
        return c


class IndexTemplateProvider:
    """
    Abstracts how the Rally index template is retrieved. Intended for testing.
    """

    def __init__(self, cfg):
        self.script_dir = cfg.opts("node", "rally.root")

    def metrics_template(self):
        return self._read("metrics-template")

    def races_template(self):
        return self._read("races-template")

    def results_template(self):
        return self._read("results-template")

    def _read(self, template_name):
        with open("%s/resources/%s.json" % (self.script_dir, template_name), encoding="utf-8") as f:
            return f.read()


class MetaInfoScope(Enum):
    """
    Defines the scope of a meta-information. Meta-information provides more context for a metric, for example the concrete version
    of Elasticsearch that has been benchmarked or environment information like CPU model or OS.
    """
    cluster = 1
    """
    Cluster level meta-information is valid for all nodes in the cluster (e.g. the benchmarked Elasticsearch version)
    """
    # host = 2
    """
    Host level meta-information is valid for all nodes on the same host (e.g. the OS name and version)
    """
    node = 3
    """
    Node level meta-information is valid for a single node (e.g. GC times)
    """


def metrics_store(cfg, read_only=True, track=None, challenge=None, car=None, meta_info=None):
    """
    Creates a proper metrics store based on the current configuration.

    :param cfg: Config object.
    :param read_only: Whether to open the metrics store only for reading (Default: True).
    :return: A metrics store implementation.
    """
    cls = metrics_store_class(cfg)
    store = cls(cfg=cfg, meta_info=meta_info)
    logging.getLogger(__name__).info("Creating %s", str(store))

    race_id = cfg.opts("system", "race.id")
    race_timestamp = cfg.opts("system", "time.start")
    selected_car = cfg.opts("mechanic", "car.names") if car is None else car

    store.open(race_id, race_timestamp, track, challenge, selected_car, create=not read_only)
    return store


def metrics_store_class(cfg):
    if cfg.opts("reporting", "datastore.type") == "elasticsearch":
        return EsMetricsStore
    else:
        return InMemoryMetricsStore


def extract_user_tags_from_config(cfg):
    """
    Extracts user tags into a structured dict

    :param cfg: The current configuration object.
    :return: A dict containing user tags. If no user tags are given, an empty dict is returned.
    """
    user_tags = cfg.opts("race", "user.tag", mandatory=False)
    return extract_user_tags_from_string(user_tags)


def extract_user_tags_from_string(user_tags):
    """
    Extracts user tags into a structured dict

    :param user_tags: A string containing user tags (tags separated by comma, key and value separated by colon).
    :return: A dict containing user tags. If no user tags are given, an empty dict is returned.
    """
    user_tags_dict = {}
    if user_tags and user_tags.strip() != "":
        try:
            for user_tag in user_tags.split(","):
                user_tag_key, user_tag_value = user_tag.split(":")
                user_tags_dict[user_tag_key] = user_tag_value
        except ValueError:
            msg = "User tag keys and values have to separated by a ':'. Invalid value [%s]" % user_tags
            logging.getLogger(__name__).exception(msg)
            raise exceptions.SystemSetupError(msg)
    return user_tags_dict


class SampleType(IntEnum):
    Warmup = 0,
    Normal = 1


class MetricsStore:
    """
    Abstract metrics store
    """

    def __init__(self, cfg, clock=time.Clock, meta_info=None):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        self._config = cfg
        self._race_id = None
        self._race_timestamp = None
        self._track = None
        self._track_params = cfg.opts("track", "params", default_value={}, mandatory=False)
        self._challenge = None
        self._car = None
        self._car_name = None
        self._environment_name = cfg.opts("system", "env.name")
        self.opened = False
        if meta_info is None:
            self._meta_info = {}
        else:
            self._meta_info = meta_info
        # ensure mandatory keys are always present
        if MetaInfoScope.cluster not in self._meta_info:
            self._meta_info[MetaInfoScope.cluster] = {}
        if MetaInfoScope.node not in self._meta_info:
            self._meta_info[MetaInfoScope.node] = {}
        self._clock = clock
        self._stop_watch = self._clock.stop_watch()
        self.logger = logging.getLogger(__name__)

    def open(self, race_id=None, race_timestamp=None, track_name=None, challenge_name=None, car_name=None, ctx=None, create=False):
        """
        Opens a metrics store for a specific race, track, challenge and car.

        :param race_id: The race id. This attribute is sufficient to uniquely identify a race.
        :param race_timestamp: The race timestamp as a datetime.
        :param track_name: Track name.
        :param challenge_name: Challenge name.
        :param car_name: Car name.
        :param ctx: An metrics store open context retrieved from another metrics store with ``#open_context``.
        :param create: True if an index should be created (if necessary). This is typically True, when attempting to write metrics and
        False when it is just opened for reading (as we can assume all necessary indices exist at this point).
        """
        if ctx:
            self._race_id = ctx["race-id"]
            self._race_timestamp = ctx["race-timestamp"]
            self._track = ctx["track"]
            self._challenge = ctx["challenge"]
            self._car = ctx["car"]
        else:
            self._race_id = race_id
            self._race_timestamp = time.to_iso8601(race_timestamp)
            self._track = track_name
            self._challenge = challenge_name
            self._car = car_name
        assert self._race_id is not None, "Attempting to open metrics store without a race id"
        assert self._race_timestamp is not None, "Attempting to open metrics store without a race timestamp"
        assert self._track is not None, "Attempting to open metrics store without a track"
        assert self._challenge is not None, "Attempting to open metrics store without a challenge"
        assert self._car is not None, "Attempting to open metrics store without a car"

        self._car_name = "+".join(self._car) if isinstance(self._car, list) else self._car

        self.logger.info("Opening metrics store for race timestamp=[%s], track=[%s], challenge=[%s], car=[%s]",
                         self._race_timestamp, self._track, self._challenge, self._car)

        user_tags = extract_user_tags_from_config(self._config)
        for k, v in user_tags.items():
            # prefix user tag with "tag_" in order to avoid clashes with our internal meta data
            self.add_meta_info(MetaInfoScope.cluster, None, "tag_%s" % k, v)
        # Don't store it for each metrics record as it's probably sufficient on race level
        # self.add_meta_info(MetaInfoScope.cluster, None, "rally_version", version.version())
        self._stop_watch.start()
        self.opened = True

    def reset_relative_time(self):
        """
        Resets the internal relative-time counter to zero.
        """
        self._stop_watch.start()

    def flush(self, refresh=True):
        """
        Explicitly flushes buffered metrics to the metric store. It is not required to flush before closing the metrics store.
        """
        raise NotImplementedError("abstract method")

    def close(self):
        self.logger.info("Closing metrics store.")
        """
        Closes the metric store. Note that it is mandatory to close the metrics store when it is no longer needed as it only persists
        metrics on close (in order to avoid additional latency during the benchmark).
        """
        self.flush()
        self.clear_meta_info()
        self.opened = False

    def add_meta_info(self, scope, scope_key, key, value):
        """
        Adds new meta information to the metrics store. All metrics entries that are created after calling this method are guaranteed to
        contain the added meta info (provided is on the same level or a level below, e.g. a cluster level metric will not contain node
        level meta information but all cluster level meta information will be contained in a node level metrics record).

        :param scope: The scope of the meta information. See MetaInfoScope.
        :param scope_key: The key within the scope. For cluster level metrics None is expected, for node level metrics the node name.
        :param key: The key of the meta information.
        :param value: The value of the meta information.
        """
        if scope == MetaInfoScope.cluster:
            self._meta_info[MetaInfoScope.cluster][key] = value
        elif scope == MetaInfoScope.node:
            if scope_key not in self._meta_info[MetaInfoScope.node]:
                self._meta_info[MetaInfoScope.node][scope_key] = {}
            self._meta_info[MetaInfoScope.node][scope_key][key] = value
        else:
            raise exceptions.SystemSetupError("Unknown meta info scope [%s]" % scope)

    def clear_meta_info(self):
        """
        Clears all internally stored meta-info. This is considered Rally internal API and not intended for normal client consumption.
        """
        self._meta_info = {
            MetaInfoScope.cluster: {},
            MetaInfoScope.node: {}
        }

    def merge_meta_info(self, to_merge):
        """
        Merges the current meta info with another one.

        :param to_merge: A meta info representation that should be merged with the current one.
        """
        if MetaInfoScope.cluster in to_merge:
            self._meta_info[MetaInfoScope.cluster].update(to_merge[MetaInfoScope.cluster])
        if MetaInfoScope.node in to_merge:
            self._meta_info[MetaInfoScope.node].update(to_merge[MetaInfoScope.node])

    @property
    def meta_info(self):
        """
        :return: All currently stored meta-info. This is considered Rally internal API and not intended for normal client consumption.
        """
        return self._meta_info

    @meta_info.setter
    def meta_info(self, meta_info):
        self._meta_info = meta_info

    @property
    def open_context(self):
        return {
            "race-id": self._race_id,
            "race-timestamp": self._race_timestamp,
            "track": self._track,
            "challenge": self._challenge,
            "car": self._car
        }

    def put_count_cluster_level(self, name, count, unit=None, task=None, operation=None, operation_type=None, sample_type=SampleType.Normal,
                                absolute_time=None, relative_time=None, meta_data=None):
        """
        Adds a new cluster level counter metric.

        :param name: The name of the metric.
        :param count: The metric value. It is expected to be of type int (otherwise use put_value_*).
        :param unit: A count may or may not have unit.
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type: Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(MetaInfoScope.cluster, None, name, count, unit, task, operation, operation_type, sample_type, absolute_time,
                         relative_time, meta_data)

    def put_count_node_level(self, node_name, name, count, unit=None, task=None, operation=None, operation_type=None,
                             sample_type=SampleType.Normal, absolute_time=None, relative_time=None, meta_data=None):
        """
        Adds a new node level counter metric.

        :param name: The name of the metric.
        :param node_name: The name of the cluster node for which this metric has been determined.
        :param count: The metric value. It is expected to be of type int (otherwise use put_value_*).
        :param unit: A count may or may not have unit.
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(MetaInfoScope.node, node_name, name, count, unit, task, operation, operation_type, sample_type, absolute_time,
                         relative_time, meta_data)

    # should be a float
    def put_value_cluster_level(self, name, value, unit, task=None, operation=None, operation_type=None, sample_type=SampleType.Normal,
                                absolute_time=None, relative_time=None, meta_data=None):
        """
        Adds a new cluster level value metric.

        :param name: The name of the metric.
        :param value: The metric value. It is expected to be of type float (otherwise use put_count_*).
        :param unit: The unit of this metric value (e.g. ms, docs/s).
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type: Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(MetaInfoScope.cluster, None, name, value, unit, task, operation, operation_type, sample_type, absolute_time,
                         relative_time, meta_data)

    def put_value_node_level(self, node_name, name, value, unit, task=None, operation=None, operation_type=None,
                             sample_type=SampleType.Normal, absolute_time=None, relative_time=None, meta_data=None):
        """
        Adds a new node level value metric.

        :param name: The name of the metric.
        :param node_name: The name of the cluster node for which this metric has been determined.
        :param value: The metric value. It is expected to be of type float (otherwise use put_count_*).
        :param unit: The unit of this metric value (e.g. ms, docs/s)
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type: Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(MetaInfoScope.node, node_name, name, value, unit, task, operation, operation_type, sample_type, absolute_time,
                         relative_time, meta_data)

    def _put_metric(self, level, level_key, name, value, unit, task, operation, operation_type, sample_type, absolute_time=None,
                    relative_time=None, meta_data=None):
        if level == MetaInfoScope.cluster:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
        elif level == MetaInfoScope.node:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
            if level_key in self._meta_info[MetaInfoScope.node]:
                meta.update(self._meta_info[MetaInfoScope.node][level_key])
        else:
            raise exceptions.SystemSetupError("Unknown meta info level [%s] for metric [%s]" % (level, name))
        if meta_data:
            meta.update(meta_data)

        if absolute_time is None:
            absolute_time = self._clock.now()
        if relative_time is None:
            relative_time = self._stop_watch.split_time()

        doc = {
            "@timestamp": time.to_epoch_millis(absolute_time),
            "relative-time": int(relative_time * 1000 * 1000),
            # TODO #777: Remove trial-* with a later release. They are only here for BWC
            "trial-id": self._race_id,
            "trial-timestamp": self._race_timestamp,
            "race-id": self._race_id,
            "race-timestamp": self._race_timestamp,
            "environment": self._environment_name,
            "track": self._track,
            "challenge": self._challenge,
            "car": self._car_name,
            "name": name,
            "value": value,
            "unit": unit,
            "sample-type": sample_type.name.lower(),
            "meta": meta
        }
        if task:
            doc["task"] = task
        if operation:
            doc["operation"] = operation
        if operation_type:
            doc["operation-type"] = operation_type
        if self._track_params:
            doc["track-params"] = self._track_params
        self._add(doc)

    def put_doc(self, doc, level=None, node_name=None, meta_data=None, absolute_time=None, relative_time=None):
        """
        Adds a new document to the metrics store. It will merge additional properties into the doc such as timestamps or track info.

        :param doc: The raw document as a ``dict``. Ownership is transferred to the metrics store (i.e. don't reuse that object).
        :param level: Whether these are cluster or node-level metrics. May be ``None`` if not applicable.
        :param node_name: The name of the node in case metrics are on node level.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        """
        if level == MetaInfoScope.cluster:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
        elif level == MetaInfoScope.node:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
            if node_name in self._meta_info[MetaInfoScope.node]:
                meta.update(self._meta_info[MetaInfoScope.node][node_name])
        elif level is None:
            meta = None
        else:
            raise exceptions.SystemSetupError("Unknown meta info level [{}]".format(level))

        if meta and meta_data:
            meta.update(meta_data)

        if absolute_time is None:
            absolute_time = self._clock.now()
        if relative_time is None:
            relative_time = self._stop_watch.split_time()

        doc.update({
            "@timestamp": time.to_epoch_millis(absolute_time),
            "relative-time": int(relative_time * 1000 * 1000),
            # TODO #777: Remove trial-* with a later release. They are only here for BWC
            "trial-id": self._race_id,
            "trial-timestamp": self._race_timestamp,
            "race-id": self._race_id,
            "race-timestamp": self._race_timestamp,
            "environment": self._environment_name,
            "track": self._track,
            "challenge": self._challenge,
            "car": self._car_name,

        })
        if meta:
            doc["meta"] = meta
        if self._track_params:
            doc["track-params"] = self._track_params

        self._add(doc)

    def bulk_add(self, memento):
        """
        Adds raw metrics store documents previously created with #to_externalizable()

        :param memento: The external representation as returned by #to_externalizable().
        """
        if memento:
            self.logger.debug("Restoring in-memory representation of metrics store.")
            for doc in pickle.loads(zlib.decompress(memento)):
                self._add(doc)

    def to_externalizable(self, clear=False):
        raise NotImplementedError("abstract method")

    def _add(self, doc):
        """
        Adds a new document to the metrics store

        :param doc: The new document.
        """
        raise NotImplementedError("abstract method")

    def get_one(self, name, sample_type=None):
        """
        Gets one value for the given metric name (even if there should be more than one).

        :param name: The metric name to query.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The corresponding value for the given metric name or None if there is no value.
        """
        return self._first_or_none(self.get(name=name, sample_type=sample_type))

    @staticmethod
    def _first_or_none(values):
        return values[0] if values else None

    def get(self, name, task=None, operation_type=None, sample_type=None):
        """
        Gets all raw values for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: A list of all values for the given metric.
        """
        return self._get(name, task, operation_type, sample_type, lambda doc: doc["value"])

    def get_raw(self, name, task=None, operation_type=None, sample_type=None, mapper=lambda doc: doc):
        """
        Gets all raw records for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param mapper A record mapper. By default, the complete record is returned.
        :return: A list of all raw records for the given metric.
        """
        return self._get(name, task, operation_type, sample_type, mapper)

    def get_unit(self, name, task=None):
        """
        Gets the unit for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :return: The corresponding unit for the given metric name or None if no metric record is available.
        """
        # does not make too much sense to ask for a sample type here
        return self._first_or_none(self._get(name, task, None, None, lambda doc: doc["unit"]))

    def _get(self, name, task, operation_type, sample_type, mapper):
        raise NotImplementedError("abstract method")

    def get_count(self, name, task=None, operation_type=None, sample_type=None):
        """

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The number of samples for this metric.
        """
        stats = self.get_stats(name, task, operation_type, sample_type)
        if stats:
            return stats["count"]
        else:
            return 0

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        """
        Gets the error rate for a specific task.

        :param task The task name to query.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: A float between 0.0 and 1.0 (inclusive) representing the error rate.
        """
        raise NotImplementedError("abstract method")

    def get_stats(self, name, task=None, operation_type=None, sample_type=None):
        """
        Gets standard statistics for the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: A metric_stats structure.
        """
        raise NotImplementedError("abstract method")

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        """
        Retrieves percentile metrics for the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param percentiles: An optional list of percentiles to show. If None is provided, by default the 99th, 99.9th and 100th percentile
        are determined. Ensure that there are enough data points in the metrics store (e.g. it makes no sense to retrieve a 99.9999
        percentile when there are only 10 values).
        :return: An ordered dictionary of the determined percentile values in ascending order. Key is the percentile, value is the
        determined value at this percentile. If no percentiles could be determined None is returned.
        """
        raise NotImplementedError("abstract method")

    def get_median(self, name, task=None, operation_type=None, sample_type=None):
        """
        Retrieves median value of the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The median value.
        """
        median = "50.0"
        percentiles = self.get_percentiles(name, task, operation_type, sample_type, percentiles=[median])
        return percentiles[median] if percentiles else None

    def get_mean(self, name, task=None, operation_type=None, sample_type=None):
        """
        Retrieves mean of the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The mean.
        """
        stats = self.get_stats(name, task, operation_type, sample_type)
        return stats["avg"] if stats else None


class EsMetricsStore(MetricsStore):
    """
    A metrics store backed by Elasticsearch.
    """
    METRICS_DOC_TYPE = "_doc"

    def __init__(self,
                 cfg,
                 client_factory_class=EsClientFactory,
                 index_template_provider_class=IndexTemplateProvider,
                 clock=time.Clock, meta_info=None):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        MetricsStore.__init__(self, cfg=cfg, clock=clock, meta_info=meta_info)
        self._index = None
        self._client = client_factory_class(cfg).create()
        self._index_template_provider = index_template_provider_class(cfg)
        self._docs = None

    def open(self, race_id=None, race_timestamp=None, track_name=None, challenge_name=None, car_name=None, ctx=None, create=False):
        self._docs = []
        MetricsStore.open(self, race_id, race_timestamp, track_name, challenge_name, car_name, ctx, create)
        self._index = self.index_name()
        # reduce a bit of noise in the metrics cluster log
        if create:
            # always update the mapping to the latest version
            self._client.put_template("rally-metrics", self._get_template())
            if not self._client.exists(index=self._index):
                self._client.create_index(index=self._index)
            else:
                self.logger.info("[%s] already exists.", self._index)
        else:
            # we still need to check for the correct index name - prefer the one with the suffix
            new_name = self._migrated_index_name(self._index)
            if self._client.exists(index=new_name):
                self._index = new_name

        # ensure we can search immediately after opening
        self._client.refresh(index=self._index)

    def index_name(self):
        ts = time.from_is8601(self._race_timestamp)
        return "rally-metrics-%04d-%02d" % (ts.year, ts.month)

    def _migrated_index_name(self, original_name):
        return "{}.new".format(original_name)

    def _get_template(self):
        return self._index_template_provider.metrics_template()

    def flush(self, refresh=True):
        if self._docs:
            self._client.bulk_index(index=self._index, doc_type=EsMetricsStore.METRICS_DOC_TYPE, items=self._docs)
            self.logger.info("Successfully added %d metrics documents for race timestamp=[%s], track=[%s], challenge=[%s], car=[%s].",
                             len(self._docs), self._race_timestamp, self._track, self._challenge, self._car)
        self._docs = []
        # ensure we can search immediately after flushing
        if refresh:
            self._client.refresh(index=self._index)

    def _add(self, doc):
        self._docs.append(doc)

    def _get(self, name, task, operation_type, sample_type, mapper):
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type)
        }
        self.logger.debug("Issuing get against index=[%s], query=[%s].", self._index, query)
        result = self._client.search(index=self._index, body=query)
        self.logger.debug("Metrics query produced [%s] results.", result["hits"]["total"])
        return [mapper(v["_source"]) for v in result["hits"]["hits"]]

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        query = {
            "query": self._query_by_name("service_time", task, operation_type, sample_type),
            "size": 0,
            "aggs": {
                "error_rate": {
                    "terms": {
                        "field": "meta.success"
                    }
                }
            }
        }
        self.logger.debug("Issuing get_error_rate against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        buckets = result["aggregations"]["error_rate"]["buckets"]
        self.logger.debug("Query returned [%d] buckets.", len(buckets))
        count_success = 0
        count_errors = 0
        for bucket in buckets:
            k = bucket["key_as_string"]
            doc_count = int(bucket["doc_count"])
            self.logger.debug("Processing key [%s] with [%d] docs.", k, doc_count)
            if k == "true":
                count_success = doc_count
            elif k == "false":
                count_errors = doc_count
            else:
                self.logger.warning("Unrecognized bucket key [%s] with [%d] docs.", k, doc_count)

        if count_errors == 0:
            return 0.0
        elif count_success == 0:
            return 1.0
        else:
            return count_errors / (count_errors + count_success)

    def get_stats(self, name, task=None, operation_type=None, sample_type=None):
        """
        Gets standard statistics for the given metric name.

        :return: A metric_stats structure. For details please refer to
        https://www.elastic.co/guide/en/elasticsearch/reference/current/search-aggregations-metrics-stats-aggregation.html
        """
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type),
            "size": 0,
            "aggs": {
                "metric_stats": {
                    "stats": {
                        "field": "value"
                    }
                }
            }
        }
        self.logger.debug("Issuing get_stats against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        return result["aggregations"]["metric_stats"]

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        if percentiles is None:
            percentiles = [99, 99.9, 100]
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type),
            "size": 0,
            "aggs": {
                "percentile_stats": {
                    "percentiles": {
                        "field": "value",
                        "percents": percentiles
                    }
                }
            }
        }
        self.logger.debug("Issuing get_percentiles against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        self.logger.debug("get_percentiles produced %d hits", hits)
        if hits > 0:
            raw = result["aggregations"]["percentile_stats"]["values"]
            return collections.OrderedDict(sorted(raw.items(), key=lambda t: float(t[0])))
        else:
            return None

    def _query_by_name(self, name, task, operation_type, sample_type):
        q = {
            "bool": {
                "filter": [
                    {
                        "term": {
                            # TODO #777: Switch to "race-id" once we remove the trial-id parameter
                            "trial-id": self._race_id
                        }
                    },
                    {
                        "term": {
                            "name": name
                        }
                    }
                ]
            }
        }
        if task:
            q["bool"]["filter"].append({
                "term": {
                    "task": task
                }
            })
        if operation_type:
            q["bool"]["filter"].append({
                "term": {
                    "operation-type": operation_type.name
                }
            })
        if sample_type:
            q["bool"]["filter"].append({
                "term": {
                    "sample-type": sample_type.name.lower()
                }
            })
        return q

    def to_externalizable(self, clear=False):
        # no need for an externalizable representation - stores everything directly
        return None

    def __str__(self):
        return "Elasticsearch metrics store"


class InMemoryMetricsStore(MetricsStore):
    def __init__(self, cfg, clock=time.Clock, meta_info=None):
        """

        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        super().__init__(cfg=cfg, clock=clock, meta_info=meta_info)
        self.docs = []

    def __del__(self):
        """
        Deletes the metrics store instance.
        """
        del self.docs

    def _add(self, doc):
        self.docs.append(doc)

    def flush(self, refresh=True):
        pass

    def to_externalizable(self, clear=False):
        docs = self.docs
        if clear:
            self.docs = []
        compressed = zlib.compress(pickle.dumps(docs))
        self.logger.debug("Compression changed size of metric store from [%d] bytes to [%d] bytes",
                         sys.getsizeof(docs, -1), sys.getsizeof(compressed, -1))
        return compressed

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        if percentiles is None:
            percentiles = [99, 99.9, 100]
        result = collections.OrderedDict()
        values = self.get(name, task, operation_type, sample_type)
        if len(values) > 0:
            sorted_values = sorted(values)
            for percentile in percentiles:
                result[percentile] = self.percentile_value(sorted_values, percentile)
        return result

    @staticmethod
    def percentile_value(sorted_values, percentile):
        """
        Calculates a percentile value for a given list of values and a percentile.

        The implementation is based on http://onlinestatbook.com/2/introduction/percentiles.html

        :param sorted_values: A sorted list of raw values for which a percentile should be calculated.
        :param percentile: A percentile between [0, 100]
        :return: the corresponding percentile value.
        """
        rank = float(percentile) / 100.0 * (len(sorted_values) - 1)
        if rank == int(rank):
            return sorted_values[int(rank)]
        else:
            lr = math.floor(rank)
            lr_next = math.ceil(rank)
            fr = rank - lr
            lower_score = sorted_values[lr]
            higher_score = sorted_values[lr_next]
            return lower_score + (higher_score - lower_score) * fr

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        error = 0
        total_count = 0
        for doc in self.docs:
            # we can use any request metrics record (i.e. service time or latency)
            if doc["name"] == "service_time" and doc["task"] == task and \
                    (operation_type is None or doc["operation-type"] == operation_type.name) and \
                    (sample_type is None or doc["sample-type"] == sample_type.name.lower()):
                total_count += 1
                if doc["meta"]["success"] is False:
                    error += 1
        if total_count > 0:
            return error / total_count
        else:
            return 0.0

    def get_stats(self, name, task=None, operation_type=None, sample_type=SampleType.Normal):
        values = self.get(name, task, operation_type, sample_type)
        sorted_values = sorted(values)
        if len(sorted_values) > 0:
            return {
                "count": len(sorted_values),
                "min": sorted_values[0],
                "max": sorted_values[-1],
                "avg": statistics.mean(sorted_values),
                "sum": sum(sorted_values)
            }
        else:
            return None

    def _get(self, name, task, operation_type, sample_type, mapper):
        return [mapper(doc)
                for doc in self.docs
                if doc["name"] == name and
                (task is None or doc["task"] == task) and
                (operation_type is None or doc["operation-type"] == operation_type.name) and
                (sample_type is None or doc["sample-type"] == sample_type.name.lower())
                ]

    def __str__(self):
        return "in-memory metrics store"


def race_store(cfg):
    """
    Creates a proper race store based on the current configuration.
    :param cfg: Config object. Mandatory.
    :return: A race store implementation.
    """
    if cfg.opts("reporting", "datastore.type") == "elasticsearch":
        logging.getLogger(__name__).info("Creating ES race store")
        return CompositeRaceStore(EsRaceStore(cfg), EsResultsStore(cfg), FileRaceStore(cfg))
    else:
        logging.getLogger(__name__).info("Creating file race store")
        return FileRaceStore(cfg)


def list_races(cfg):
    def format_dict(d):
        if d:
            items=sorted(d.items())
            return ", ".join(["%s=%s" % (k, v) for k, v in items])
        else:
            return None

    races = []
    for race in race_store(cfg).list():
        races.append([race.race_id, time.to_iso8601(race.race_timestamp), race.track, format_dict(race.track_params), race.challenge_name, race.car_name,
                      format_dict(race.user_tags), race.track_revision, race.team_revision])

    if len(races) > 0:
        console.println("\nRecent races:\n")
        console.println(tabulate.tabulate(races, headers=["Race ID", "Race Timestamp", "Track", "Track Parameters", "Challenge", "Car", "User Tags", "Track Revision", "Team Revision"]))
    else:
        console.println("")
        console.println("No recent races found.")


def create_race(cfg, track, challenge, track_revision=None):
    car = cfg.opts("mechanic", "car.names")
    environment = cfg.opts("system", "env.name")
    race_id = cfg.opts("system", "race.id")
    race_timestamp = cfg.opts("system", "time.start")
    user_tags = extract_user_tags_from_config(cfg)
    pipeline = cfg.opts("race", "pipeline")
    track_params = cfg.opts("track", "params")
    car_params = cfg.opts("mechanic", "car.params")
    plugin_params = cfg.opts("mechanic", "plugin.params")
    rally_version = version.version()

    return Race(rally_version, environment, race_id, race_timestamp, pipeline, user_tags, track, track_params,
                challenge, car, car_params, plugin_params, track_revision)


class Race:
    def __init__(self, rally_version, environment_name, race_id, race_timestamp, pipeline, user_tags, track,
                 track_params, challenge, car, car_params, plugin_params, track_revision=None, team_revision=None,
                 distribution_version=None, distribution_flavor=None, revision=None, results=None):
        if results is None:
            results = {}
        self.rally_version = rally_version
        self.environment_name = environment_name
        self.race_id = race_id
        self.race_timestamp = race_timestamp
        self.pipeline = pipeline
        self.user_tags = user_tags
        self.track = track
        self.track_params = track_params
        self.challenge = challenge
        self.car = car
        self.car_params = car_params
        self.plugin_params = plugin_params
        self.track_revision = track_revision
        self.team_revision = team_revision
        self.distribution_version = distribution_version
        self.distribution_flavor = distribution_flavor
        self.revision = revision
        self.results = results

    @property
    def track_name(self):
        return str(self.track)

    @property
    def challenge_name(self):
        return str(self.challenge) if self.challenge else None

    @property
    def car_name(self):
        return "+".join(self.car) if isinstance(self.car, list) else self.car

    @property
    def meta_data(self):
        meta = {}
        meta.update(self.track.meta_data)
        if self.challenge:
            meta.update(self.challenge.meta_data)
        return meta

    def add_results(self, results):
        self.results = results

    def as_dict(self):
        """
        :return: A dict representation suitable for persisting this race instance as JSON.
        """
        d = {
            "rally-version": self.rally_version,
            "environment": self.environment_name,
            # TODO #777: Remove trial-* with a later release. They are only here for BWC
            "trial-id": self.race_id,
            "trial-timestamp": time.to_iso8601(self.race_timestamp),
            "race-id": self.race_id,
            "race-timestamp": time.to_iso8601(self.race_timestamp),
            "pipeline": self.pipeline,
            "user-tags": self.user_tags,
            "track": self.track_name,
            "car": self.car,
            "cluster": {
                "revision": self.revision,
                "distribution-version": self.distribution_version,
                "distribution-flavor": self.distribution_flavor,
                "team-revision": self.team_revision,
            },
            "results": self.results.as_dict()
        }
        if self.track_revision:
            d["track-revision"] = self.track_revision
        if not self.challenge.auto_generated:
            d["challenge"] = self.challenge_name
        if self.track_params:
            d["track-params"] = self.track_params
        if self.car_params:
            d["car-params"] = self.car_params
        if self.plugin_params:
            d["plugin-params"] = self.plugin_params
        return d

    def to_result_dicts(self):
        """
        :return: a list of dicts, suitable for persisting the results of this race in a format that is Kibana-friendly.
        """
        result_template = {
            "rally-version": self.rally_version,
            "environment": self.environment_name,
            # TODO #777: Remove trial-* with a later release. They are only here for BWC
            "trial-id": self.race_id,
            "trial-timestamp": time.to_iso8601(self.race_timestamp),
            "race-id": self.race_id,
            "race-timestamp": time.to_iso8601(self.race_timestamp),
            "distribution-version": self.distribution_version,
            "distribution-flavor": self.distribution_flavor,
            "distribution-major-version": versions.major_version(self.distribution_version),
            "user-tags": self.user_tags,
            "track": self.track_name,
            "car": self.car_name,
            # allow to logically delete records, e.g. for UI purposes when we only want to show the latest result
            "active": True
        }
        if self.team_revision:
            result_template["team-revision"] = self.team_revision
        if self.track_revision:
            result_template["track-revision"] = self.track_revision
        if not self.challenge.auto_generated:
            result_template["challenge"] = self.challenge_name
        if self.track_params:
            result_template["track-params"] = self.track_params
        if self.car_params:
            result_template["car-params"] = self.car_params
        if self.plugin_params:
            result_template["plugin-params"] = self.plugin_params
        if self.meta_data:
            result_template["meta"] = self.meta_data

        all_results = []

        for item in self.results.as_flat_list():
            result = result_template.copy()
            result.update(item)
            all_results.append(result)

        return all_results

    @classmethod
    def from_dict(cls, d):
        # for backwards compatibility with Rally < 0.9.2
        if "user-tag" in d:
            user_tags = extract_user_tags_from_string(d["user-tag"])
        elif "user-tags" in d:
            user_tags = d["user-tags"]
        else:
            user_tags = {}
        # TODO: cluster is optional for BWC. This can be removed after some grace period.
        # TODO #777: Remove the backwards-compatibility layer with trial*
        cluster = d.get("cluster", {})
        return Race(d["rally-version"], d["environment"], d.get("race-id", d.get("trial-id")),
                    time.from_is8601(d.get("race-timestamp", d.get("trial-timestamp"))),
                    d["pipeline"], user_tags, d["track"], d.get("track-params"), d.get("challenge"), d["car"],
                    d.get("car-params"), d.get("plugin-params"), track_revision=d.get("track-revision"),
                    team_revision=cluster.get("team-revision"),
                    distribution_version=cluster.get("distribution-version"),
                    distribution_flavor=cluster.get("distribution-flavor"),
                    revision=cluster.get("revision"), results=d["results"])


class RaceStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.environment_name = cfg.opts("system", "env.name")
        self.race_timestamp = cfg.opts("system", "time.start")
        self.race_id = cfg.opts("system", "race.id")
        self.current_race = None

    def find_by_race_id(self, race_id):
        raise NotImplementedError("abstract method")

    def list(self):
        raise NotImplementedError("abstract method")

    def store_race(self, race):
        self._store(race.as_dict())

    def _store(self, doc):
        raise NotImplementedError("abstract method")

    def _max_results(self):
        return int(self.cfg.opts("system", "list.races.max_results"))


# Does not inherit from RaceStore as it is only a delegator with the same API.
class CompositeRaceStore:
    """
    Internal helper class to store races as file and to Elasticsearch in case users want Elasticsearch as a race store.

    It provides the same API as RaceStore. It delegates writes to all stores and all read operations only the Elasticsearch race store.
    """
    def __init__(self, es_store, es_results_store, file_store):
        self.es_store = es_store
        self.es_results_store = es_results_store
        self.file_store = file_store

    def find_by_race_id(self, race_id):
        return self.es_store.find_by_race_id(race_id)

    def store_race(self, race):
        self.file_store.store_race(race)
        self.es_store.store_race(race)
        self.es_results_store.store_results(race)

    def list(self):
        return self.es_store.list()


class FileRaceStore(RaceStore):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.races_path = paths.races_root(self.cfg)
        self.race_path = paths.race_root(self.cfg)

    def _store(self, doc):
        import json
        io.ensure_dir(self.race_path)
        with open(self._race_file(), mode="wt", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=True, ensure_ascii=False))

    def _race_file(self, race_id=None):
        return os.path.join(paths.race_root(cfg=self.cfg, race_id=race_id), "race.json")

    def list(self):
        import glob
        results = glob.glob(self._race_file(race_id="*"))
        all_races = self._to_races(results)
        return all_races[:self._max_results()]

    def find_by_race_id(self, race_id):
        race_file = self._race_file(race_id=race_id)
        if io.exists(race_file):
            races = self._to_races([race_file])
            if races:
                return races[0]
        raise exceptions.NotFound("No race with race id [{}]".format(race_id))

    def _to_races(self, results):
        import json
        races = []
        for result in results:
            # noinspection PyBroadException
            try:
                with open(result, mode="rt", encoding="utf-8") as f:
                    races.append(Race.from_dict(json.loads(f.read())))
            except BaseException:
                logging.getLogger(__name__).exception("Could not load race file [%s] (incompatible format?) Skipping...", result)
        return sorted(races, key=lambda r: r.race_timestamp, reverse=True)


class EsRaceStore(RaceStore):
    INDEX_PREFIX = "rally-races-"
    RACE_DOC_TYPE = "_doc"

    def __init__(self, cfg, client_factory_class=EsClientFactory, index_template_provider_class=IndexTemplateProvider):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        """
        super().__init__(cfg)
        self.client = client_factory_class(cfg).create()
        self.index_template_provider = index_template_provider_class(cfg)

    def _store(self, doc):
        # always update the mapping to the latest version
        self.client.put_template("rally-races", self.index_template_provider.races_template())
        self.client.index(index=self.index_name(), doc_type=EsRaceStore.RACE_DOC_TYPE, item=doc)

    def index_name(self):
        return "%s%04d-%02d" % (EsRaceStore.INDEX_PREFIX, self.race_timestamp.year, self.race_timestamp.month)

    def list(self):
        filters = [{
            "term": {
                "environment": self.environment_name
            }
        }]

        query = {
            "query": {
                "bool": {
                    "filter": filters
                }
            },
            "size": self._max_results(),
            "sort": [
                {
                    # TODO #777: Switch to "race-timestamp" once we remove the trial-timestamp parameter
                    "trial-timestamp": {
                        "order": "desc"
                    }
                }
            ]
        }
        result = self.client.search(index="%s*" % EsRaceStore.INDEX_PREFIX, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        if hits > 0:
            return [Race.from_dict(v["_source"]) for v in result["hits"]["hits"]]
        else:
            return []

    def find_by_race_id(self, race_id):
        filters = [{
                "term": {
                    "environment": self.environment_name
                }
            },
            {
                "term": {
                    # TODO #777: Switch to "race-id" once we remove the trial-id parameter
                    "trial-id": race_id
                }
            }]

        query = {
            "query": {
                "bool": {
                    "filter": filters
                }
            }
        }
        result = self.client.search(index="%s*" % EsRaceStore.INDEX_PREFIX, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        if hits == 1:
            return Race.from_dict(result["hits"]["hits"][0]["_source"])
        elif hits > 1:
            raise exceptions.RallyAssertionError(
                "Expected exactly one race to match race id [{}] but there were [{}] matches.".format(race_id, hits))
        else:
            raise exceptions.NotFound("No race with race id [{}]".format(race_id))


class EsResultsStore:
    """
    Stores the results of a race in a format that is better suited for reporting with Kibana.
    """
    INDEX_PREFIX = "rally-results-"
    RESULTS_DOC_TYPE = "_doc"

    def __init__(self, cfg, client_factory_class=EsClientFactory, index_template_provider_class=IndexTemplateProvider):
        """
        Creates a new results store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        """
        self.cfg = cfg
        self.race_timestamp = cfg.opts("system", "time.start")
        self.client = client_factory_class(cfg).create()
        self.index_template_provider = index_template_provider_class(cfg)

    def store_results(self, race):
        # always update the mapping to the latest version
        self.client.put_template("rally-results", self.index_template_provider.results_template())
        self.client.bulk_index(index=self.index_name(), doc_type=EsResultsStore.RESULTS_DOC_TYPE, items=race.to_result_dicts())

    def index_name(self):
        return "%s%04d-%02d" % (EsResultsStore.INDEX_PREFIX, self.race_timestamp.year, self.race_timestamp.month)
