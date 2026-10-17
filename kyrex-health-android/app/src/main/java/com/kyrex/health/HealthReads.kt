package com.kyrex.health

import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlin.reflect.KClass

// Filter in the provider before Jetpack decodes records. Filtering only after
// readRecords lets another source's incompatible record abort a Samsung sync.
internal fun <T : Record> samsungReadRequest(
    type: KClass<T>, filter: TimeRangeFilter, pageToken: String? = null,
): ReadRecordsRequest<T> = ReadRecordsRequest(
    recordType = type,
    timeRangeFilter = filter,
    dataOriginFilter = setOf(DataOrigin(SAMSUNG_ORIGIN)),
    pageToken = pageToken,
)
