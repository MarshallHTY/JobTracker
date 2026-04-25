CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    role_title TEXT NOT NULL,
    role_type TEXT,
    location TEXT,
    visa_sponsorship TEXT CHECK (visa_sponsorship IN ('yes', 'no', 'unknown')) DEFAULT 'unknown',
    source TEXT,
    job_link TEXT,
    date_found TEXT,
    date_applied TEXT,
    apply_by_date TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'saved',
            'drafting',
            'applied',
            'assessment',
            'interview',
            'offer',
            'rejected',
            'ghosted',
            'withdrawn'
        )
    ) DEFAULT 'saved',
    cv_version TEXT,
    cl_version TEXT,
    follow_up_date TEXT,
    priority INTEGER CHECK (priority BETWEEN 1 AND 5) DEFAULT 3,
    fit_score REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trg_applications_updated_at
AFTER UPDATE ON applications
FOR EACH ROW
BEGIN
    UPDATE applications
    SET updated_at = datetime('now')
    WHERE id = OLD.id;
END;
