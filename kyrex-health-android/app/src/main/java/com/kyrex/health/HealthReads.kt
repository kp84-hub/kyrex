package com.kyrex.health

import android.annotation.SuppressLint
import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectClient.Companion.SDK_AVAILABLE
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import kotlin.reflect.KClass

// Lint 8.10.1 mis-resolves the SDK 1.2 nested IntDef constants: it rejects
// SDK_AVAILABLE while listing that exact constant as allowed. Limit the
// workaround to this comparison; retain the actual provider availability gate.
@SuppressLint("WrongConstant")
internal fun healthAvailable(context: Context): Boolean =
    HealthConnectClient.getSdkStatus(context) == SDK_AVAILABLE

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
