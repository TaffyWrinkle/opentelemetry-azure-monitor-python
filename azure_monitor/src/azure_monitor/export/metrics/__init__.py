# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
import json
import logging
from typing import Sequence
from urllib.parse import urlparse

from opentelemetry.metrics import Metric
from opentelemetry.util import time_ns
from opentelemetry.sdk.metrics import Counter, Observer
from opentelemetry.sdk.metrics.export import (
    MetricRecord,
    MetricsExporter,
    MetricsExportResult,
)
from opentelemetry.sdk.util import ns_to_iso_str

from azure_monitor import protocol, utils
from azure_monitor.export import (
    BaseExporter,
    ExportResult,
    get_metrics_export_result,
)

logger = logging.getLogger(__name__)


class AzureMonitorMetricsExporter(BaseExporter, MetricsExporter):
    def export(
        self, metric_records: Sequence[MetricRecord]
    ) -> MetricsExportResult:
        envelopes = list(map(self.metric_to_envelope, metric_records))
        envelopes = list(
            map(
                lambda x: x.to_dict(),
                self.apply_telemetry_processors(envelopes),
            )
        )
        try:
            result = self._transmit(envelopes)
            if result == ExportResult.FAILED_RETRYABLE:
                self.storage.put(envelopes, result)
            if result == ExportResult.SUCCESS:
                # Try to send any cached events
                self._transmit_from_storage()
            return get_metrics_export_result(result)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Exception occurred while exporting the data.")
            return get_metrics_export_result(ExportResult.FAILED_NOT_RETRYABLE)

    def metric_to_envelope(
        self, metric_record: MetricRecord
    ) -> protocol.Envelope:

        if not metric_record:
            return None
        # TODO: Opentelemetry does not have last updated timestamp for observer
        # type metrics yet.
        _time = time_ns()
        if isinstance(metric_record.metric, Metric):
            _time = metric_record.metric.bind(
                    metric_record.label_set
                ).last_update_timestamp
        envelope = protocol.Envelope(
            ikey=self.options.instrumentation_key,
            tags=dict(utils.azure_monitor_context),
            time=ns_to_iso_str(_time),
        )
        envelope.name = "Microsoft.ApplicationInsights.Metric"
        value = 0
        metric = metric_record.metric
        if isinstance(metric, Counter):
            value = metric_record.aggregator.checkpoint
        elif isinstance(metric, Observer):
            value = metric_record.aggregator.checkpoint.last
            if not value:
                value = 0
        else:
            # TODO: What do measure aggregations look like in AI?
            logger.warning("Measure metric recorded.")

        data_point = protocol.DataPoint(
            ns=metric_record.metric.description,
            name=metric_record.metric.name,
            value=value,
            kind=protocol.DataPointType.MEASUREMENT.value,
        )

        properties = {}
        for label_tuple in metric_record.label_set.labels:
            properties[label_tuple[0]] = label_tuple[1]
        data = protocol.MetricData(metrics=[data_point], properties=properties)
        envelope.data = protocol.Data(base_data=data, base_type="MetricData")
        return envelope
