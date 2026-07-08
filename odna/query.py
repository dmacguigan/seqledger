"""Read-only lookups over the catalog (CLI convenience)."""


def find_by_uniq_id(conn, uniq_id):
    return conn.execute(
        """SELECT s.project_id, s.sample_id, s.taxon, s.uniq_id
           FROM samples s WHERE s.uniq_id = ? ORDER BY s.project_id""",
        (uniq_id,)).fetchall()


def find_sample(conn, term):
    like = f"%{term}%"
    return conn.execute(
        """SELECT project_id, sample_id, taxon, uniq_id FROM samples
           WHERE sample_id LIKE ? OR taxon LIKE ? OR uniq_id LIKE ?
           ORDER BY project_id, sample_id""",
        (like, like, like)).fetchall()


def unbacked_projects(conn):
    """Projects with no verified pdrive backup."""
    return conn.execute(
        """SELECT p.project_id,
                  COALESCE(b.verified, 0) AS verified,
                  b.n_files, b.n_mismatch
           FROM projects p
           LEFT JOIN backups b ON b.project_id = p.project_id AND b.location = 'pdrive'
           WHERE COALESCE(b.verified, 0) = 0
           ORDER BY p.project_id""").fetchall()


def mismatched_files(conn):
    return conn.execute(
        """SELECT project_id, filename, store_md5, pdrive_md5
           FROM files WHERE md5_match = 0 ORDER BY project_id, filename""").fetchall()


def project_summary(conn):
    return conn.execute(
        """SELECT p.project_id, p.source, p.description,
                  (SELECT COUNT(*) FROM samples s WHERE s.project_id = p.project_id) AS n_samples,
                  (SELECT COUNT(*) FROM files f WHERE f.project_id = p.project_id) AS n_files,
                  COALESCE(p.data_check_status, 'unchecked') AS data_check_status,
                  p.data_check_n_missing, p.data_check_n_orphan,
                  (SELECT COUNT(*) FROM files f
                     WHERE f.project_id = p.project_id AND f.md5_match = 0) AS n_mismatch,
                  (SELECT COUNT(*) FROM files f
                     WHERE f.project_id = p.project_id AND f.md5_match IS NULL) AS n_uncompared,
                  p.owner_name, p.seq_data_relpath, p.seqdata_root, p.date_ingested
           FROM projects p
           ORDER BY p.project_id""").fetchall()
