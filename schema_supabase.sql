-- WorkTime Tracker — Supabase cloud sync schema
-- Run this in Supabase Dashboard → SQL Editor.
--
-- Creates two tables for daily aggregate snapshots and RLS policies
-- that allow the publishable (anon) key to read/write all rows.
-- (No Supabase Auth login; device_id separates data per device.)

-- ============================================================
-- Table 1: tag_time_records_cloud
-- Per-device daily tag totals (e.g. 2026-08-19 / Work / 14400s)
-- ============================================================
CREATE TABLE IF NOT EXISTS tag_time_records_cloud (
    id          BIGSERIAL PRIMARY KEY,
    device_id   TEXT NOT NULL,
    date        DATE NOT NULL,
    tag         TEXT NOT NULL,
    seconds     REAL NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(device_id, date, tag)
);

-- ============================================================
-- Table 2: time_records_cloud
-- Per-device daily app/project totals
-- ============================================================
CREATE TABLE IF NOT EXISTS time_records_cloud (
    id           BIGSERIAL PRIMARY KEY,
    device_id    TEXT NOT NULL,
    date         DATE NOT NULL,
    process_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    tag          TEXT NOT NULL DEFAULT 'Other',
    seconds      REAL NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(device_id, date, process_name, project)
);

-- ============================================================
-- Row Level Security
-- ============================================================
-- Enable RLS on both tables. Without policies, all access is denied.
ALTER TABLE tag_time_records_cloud ENABLE ROW LEVEL SECURITY;
ALTER TABLE time_records_cloud ENABLE ROW LEVEL SECURITY;

-- Allow the anon role (publishable key) full read/write access.
-- This is safe ONLY if the publishable key is not distributed publicly.
-- For a personal desktop app used on your own machines, this is acceptable.
CREATE POLICY "anon full access tag_time_records_cloud"
    ON tag_time_records_cloud
    FOR ALL TO anon
    USING (true)
    WITH CHECK (true);

CREATE POLICY "anon full access time_records_cloud"
    ON time_records_cloud
    FOR ALL TO anon
    USING (true)
    WITH CHECK (true);

-- ============================================================
-- Indexes for sync queries
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_tag_cloud_device_date
    ON tag_time_records_cloud (device_id, date);

CREATE INDEX IF NOT EXISTS idx_time_cloud_device_date
    ON time_records_cloud (device_id, date);
