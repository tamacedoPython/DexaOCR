-- =============================================================================
-- DexaOCR Worker — AgileAI Database DDL
-- Target database: AgileAI (SQL Server 2019+)
-- Created: 2026-04-23
-- Purpose: Audit and traceability tables for the DexaOCR Worker service.
--
-- Tables:
--   AgileAI.dbo.WorkerRequests  — one row per request, full lifecycle
--   AgileAI.dbo.WorkerEvents    — event log / audit trail per request
--
-- Notes:
--   - Designed to be reused by future Workers (WorkerName column scopes rows)
--   - Run this script once against the AgileAI database
--   - All timestamps stored as DATETIMEOFFSET to preserve UTC context
-- =============================================================================

USE AgileAI;
GO

-- =============================================================================
-- Table: WorkerRequests
-- One row per request received from Orchestra.
-- Updated as the request progresses through its lifecycle.
-- =============================================================================

IF NOT EXISTS (
    SELECT 1 FROM sys.objects
    WHERE object_id = OBJECT_ID(N'[dbo].[WorkerRequests]')
      AND type = N'U'
)
BEGIN
    CREATE TABLE [dbo].[WorkerRequests] (
        -- Identity
        [ID]                        INT                 NOT NULL IDENTITY(1,1),
        [RequestID]                 NVARCHAR(128)       NOT NULL,   -- UUID from Orchestra
        [CorrelationID]             NVARCHAR(128)       NOT NULL,   -- Trace correlation
        [WorkerName]                NVARCHAR(64)        NOT NULL,   -- e.g. "DexaOCR"

        -- Clinical context
        [StudyUID]                  NVARCHAR(256)       NOT NULL,
        [PatientID]                 NVARCHAR(128)       NULL,
        [Modality]                  NVARCHAR(32)        NULL,
        [ExamType]                  NVARCHAR(128)       NULL,

        -- Lifecycle status
        [Status]                    NVARCHAR(32)        NOT NULL    -- RECEIVED | PROCESSING | SUCCESS | ERROR | PUBLISHED | PUBLISH_FAILED
            CONSTRAINT [CK_WorkerRequests_Status]
            CHECK ([Status] IN ('RECEIVED','PROCESSING','SUCCESS','ERROR','PUBLISHED','PUBLISH_FAILED')),

        -- Timestamps
        [ReceivedAt]                DATETIMEOFFSET(3)   NOT NULL,
        [ProcessingStartedAt]       DATETIMEOFFSET(3)   NULL,
        [ProcessingFinishedAt]      DATETIMEOFFSET(3)   NULL,
        [ProcessingTimeMs]          INT                 NULL,

        -- Output
        [OutputJson]                NVARCHAR(MAX)       NULL,   -- Full structured JSON output

        -- Error info
        [ErrorType]                 NVARCHAR(128)       NULL,
        [ErrorMessage]              NVARCHAR(2000)      NULL,

        -- RabbitMQ publication
        [PublishedToRabbitMQ]       BIT                 NOT NULL DEFAULT 0,
        [PublishedAt]               DATETIMEOFFSET(3)   NULL,
        [PublishError]              NVARCHAR(1000)      NULL,

        -- Audit
        [CreatedAt]                 DATETIMEOFFSET(3)   NOT NULL DEFAULT SYSDATETIMEOFFSET(),
        [UpdatedAt]                 DATETIMEOFFSET(3)   NOT NULL DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT [PK_WorkerRequests] PRIMARY KEY CLUSTERED ([ID] ASC),
        CONSTRAINT [UQ_WorkerRequests_RequestID] UNIQUE ([RequestID])
    );

    -- Index for lookups by StudyUID (common in operational queries)
    CREATE NONCLUSTERED INDEX [IX_WorkerRequests_StudyUID]
        ON [dbo].[WorkerRequests] ([StudyUID]);

    -- Index for filtering by worker + status (dashboards / monitoring)
    CREATE NONCLUSTERED INDEX [IX_WorkerRequests_WorkerStatus]
        ON [dbo].[WorkerRequests] ([WorkerName], [Status], [ReceivedAt] DESC);

    -- Index for correlation tracing
    CREATE NONCLUSTERED INDEX [IX_WorkerRequests_CorrelationID]
        ON [dbo].[WorkerRequests] ([CorrelationID]);

    PRINT 'Table [dbo].[WorkerRequests] created.';
END
ELSE
BEGIN
    PRINT 'Table [dbo].[WorkerRequests] already exists — skipped.';
END
GO

-- =============================================================================
-- Table: WorkerEvents
-- Immutable audit trail.  One row per lifecycle event per request.
-- Allows chronological reconstruction of what happened to each request.
-- =============================================================================

IF NOT EXISTS (
    SELECT 1 FROM sys.objects
    WHERE object_id = OBJECT_ID(N'[dbo].[WorkerEvents]')
      AND type = N'U'
)
BEGIN
    CREATE TABLE [dbo].[WorkerEvents] (
        -- Identity
        [ID]                        INT                 NOT NULL IDENTITY(1,1),
        [RequestID]                 NVARCHAR(128)       NOT NULL,
        [CorrelationID]             NVARCHAR(128)       NOT NULL,
        [WorkerName]                NVARCHAR(64)        NOT NULL,

        -- Event classification
        [Stage]                     NVARCHAR(64)        NOT NULL,
            -- MESSAGE_RECEIVED | VALIDATION_OK | VALIDATION_FAILED
            -- PROCESSING_STARTED | PROCESSING_SUCCESS | PROCESSING_FAILED
            -- PUBLISH_STARTED | PUBLISH_SUCCESS | PUBLISH_FAILED
            -- ACK_SENT | NACK_SENT
        [Status]                    NVARCHAR(16)        NOT NULL
            CONSTRAINT [CK_WorkerEvents_Status]
            CHECK ([Status] IN ('OK','FAILED','INFO','WARNING')),

        -- Human-readable content
        [Message]                   NVARCHAR(500)       NULL,
        [Details]                   NVARCHAR(2000)      NULL,

        -- Timestamp
        [EventAt]                   DATETIMEOFFSET(3)   NOT NULL DEFAULT SYSDATETIMEOFFSET(),

        CONSTRAINT [PK_WorkerEvents] PRIMARY KEY CLUSTERED ([ID] ASC)
    );

    -- Primary lookup: all events for a request in order
    CREATE NONCLUSTERED INDEX [IX_WorkerEvents_RequestID]
        ON [dbo].[WorkerEvents] ([RequestID], [EventAt] ASC);

    -- Worker-level monitoring
    CREATE NONCLUSTERED INDEX [IX_WorkerEvents_WorkerStage]
        ON [dbo].[WorkerEvents] ([WorkerName], [Stage], [EventAt] DESC);

    PRINT 'Table [dbo].[WorkerEvents] created.';
END
ELSE
BEGIN
    PRINT 'Table [dbo].[WorkerEvents] already exists — skipped.';
END
GO

-- =============================================================================
-- Verification queries (run manually to validate)
-- =============================================================================

-- SELECT TOP 10 * FROM [dbo].[WorkerRequests] ORDER BY CreatedAt DESC;
-- SELECT TOP 50 * FROM [dbo].[WorkerEvents]   ORDER BY EventAt DESC;

-- Full lifecycle view for a specific request:
-- SELECT r.*, e.Stage, e.Status, e.Message, e.EventAt
-- FROM [dbo].[WorkerRequests] r
-- JOIN [dbo].[WorkerEvents]   e ON r.RequestID = e.RequestID
-- WHERE r.RequestID = '<your-request-id>'
-- ORDER BY e.EventAt;

PRINT 'DDL script completed successfully.';
GO
