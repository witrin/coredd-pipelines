-- DECLARE since TIMESTAMP DEFAULT '2025-07-01 00:00:00 UTC';
-- DECLARE until TIMESTAMP DEFAULT '2025-07-02 00:00:00 UTC';
-- DECLARE languages ARRAY<STRING> DEFAULT ['TypeScript', 'Python', 'JavaScript'];

WITH
  time_window AS (
    SELECT TIMESTAMP_DIFF(@until, @since, DAY) AS window_days
  ),
  pulls AS (
    SELECT
      repo.name AS repository,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS change_number,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.user.login') AS change_author,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.ref') AS base_ref,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.repo.default_branch') AS default_branch
    FROM `githubarchive.day.20*`
    WHERE
      _TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                       AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND type = 'PullRequestEvent'
      AND JSON_EXTRACT_SCALAR(payload, '$.action') = 'opened'
      AND created_at >= @since
      AND created_at < @until
      AND JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.repo.language')
        IN UNNEST(@languages)
      AND JSON_EXTRACT_SCALAR(payload, '$.pull_request.user.type') = 'User'
  ),
  default_target AS (
    SELECT
      repository,
      SAFE_DIVIDE(COUNTIF(base_ref = default_branch), COUNT(*)) AS default_target_share
    FROM pulls
    GROUP BY repository
  ),
  changes AS (
    SELECT
      repository,
      change_number,
      change_author
    FROM pulls
    WHERE base_ref = default_branch
  ),
  reviews AS (
    SELECT
      repo.name AS repository,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS change_number,
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.user.login') AS change_author,
      JSON_EXTRACT_SCALAR(payload, '$.review.user.login') AS reviewer,
      created_at AS review_created_at
    FROM `githubarchive.day.20*`
    WHERE
      _TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                       AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND type = 'PullRequestReviewEvent'
      AND JSON_EXTRACT_SCALAR(payload, '$.action') = 'created'
      AND created_at >= @since
      AND created_at < @until
      AND JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.repo.language')
        IN UNNEST(@languages)
      AND JSON_EXTRACT_SCALAR(payload, '$.pull_request.user.type') = 'User'
      AND JSON_EXTRACT_SCALAR(payload, '$.review.user.type') = 'User'
      AND JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.ref')
          = JSON_EXTRACT_SCALAR(payload, '$.pull_request.base.repo.default_branch')
  ),
  per_change_base AS (
    SELECT
      p.repository,
      p.change_number,
      p.change_author,
      SUM(
        CASE
          WHEN r.reviewer IS NOT NULL AND r.reviewer != p.change_author THEN 1
          ELSE 0
        END
      ) AS review_count,
      SUM(
        CASE
          WHEN r.reviewer = p.change_author THEN 1
          ELSE 0
        END
      ) AS audit_count,
      COUNT(
        DISTINCT
          CASE
            WHEN r.reviewer IS NOT NULL AND r.reviewer != p.change_author
              THEN r.reviewer
          END
      ) AS count
    FROM changes p
    LEFT JOIN reviews r
      ON p.repository = r.repository AND p.change_number = r.change_number
    GROUP BY p.repository, p.change_number, p.change_author
  ),
  per_change AS (
    SELECT
      repository,
      change_number,
      change_author,
      review_count,
      count,
      audit_count,
      review_count > 0 AS is_reviewed,
      (audit_count > 0 AND review_count = 0) AS is_audit_only,
      (review_count = 0 AND audit_count = 0) AS is_no_review
    FROM per_change_base
  ),
  per_repository AS (
    SELECT
      pc.repository,
      COUNT(*) AS total_changes,
      SUM(pc.review_count) AS total_reviews,
      COUNT(
        DISTINCT
          CASE
            WHEN r.reviewer IS NOT NULL AND r.reviewer != pc.change_author
              THEN r.reviewer
          END
      ) AS total_reviewers,
      APPROX_QUANTILES(pc.review_count, 2)[OFFSET(1)]
        AS median_reviews_per_change,
      APPROX_QUANTILES(pc.count, 2)[OFFSET(1)] AS median_reviewers_per_change,
      COUNTIF(pc.is_reviewed), COUNT(*) AS review_changes,
      COUNTIF(pc.is_audit_only), COUNT(*) AS audit_changes
    FROM per_change pc
    LEFT JOIN reviews r
      ON pc.repository = r.repository AND pc.change_number = r.change_number
    GROUP BY pc.repository
  ),
  filtered AS (
    SELECT *
    FROM per_repository
    WHERE total_reviewers >= 2 AND total_changes >= 1 AND total_reviews >= 1
  ),
  resolutions AS (
    SELECT
      i.repo.name AS repository,
      JSON_EXTRACT_SCALAR(i.payload, '$.issue.number') AS issue_number,
      ARRAY(
        SELECT JSON_EXTRACT_SCALAR(l, '$.name')
        FROM UNNEST(JSON_EXTRACT_ARRAY(i.payload, '$.issue.labels')) AS l
      ) AS labels
    FROM `githubarchive.day.20*` AS i
    JOIN filtered f
      ON i.repo.name = f.repository
    WHERE
      i._TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                          AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND i.type = 'IssuesEvent'
      AND JSON_EXTRACT_SCALAR(i.payload, '$.action') = 'closed'
      AND i.created_at >= @since
      AND i.created_at < @until
  ),
  per_repository_resolutions AS (
    SELECT
      repository,
      COUNT(DISTINCT issue_number) AS total_resolutions,
      SAFE_DIVIDE(
        COUNT(DISTINCT IF(ARRAY_LENGTH(labels) > 0, issue_number, NULL)),
        COUNT(DISTINCT issue_number)
      ) AS label_share
    FROM resolutions
    GROUP BY repository
  ),
  issues AS (
    SELECT
      i.repo.name AS repository,
      JSON_EXTRACT_SCALAR(i.payload, '$.issue.number') AS issue_number
    FROM `githubarchive.day.20*` AS i
    JOIN filtered f
      ON i.repo.name = f.repository
    WHERE
      i._TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                          AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND i.type = 'IssuesEvent'
      AND JSON_EXTRACT_SCALAR(i.payload, '$.action') = 'opened'
      AND i.created_at >= @since
      AND i.created_at < @until
  ),
  per_repository_issues AS (
    SELECT
      repository,
      COUNT(DISTINCT issue_number) AS total_issues
    FROM issues
    GROUP BY repository
  ),
  merges AS (
    SELECT
      p.repo.name AS repository,
      JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.number') AS change_number,
      JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.user.login') AS change_author,
      p.created_at AS merged_at
    FROM `githubarchive.day.20*` AS p
    JOIN filtered f
      ON p.repo.name = f.repository
    WHERE
      p._TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                          AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND p.type = 'PullRequestEvent'
      AND JSON_EXTRACT_SCALAR(p.payload, '$.action') = 'closed'
      AND JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.merged') = 'true'
      AND JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.base.ref')
          = JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.base.repo.default_branch')
      AND p.created_at >= @since
      AND p.created_at < @until
      AND JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.base.repo.language')
        IN UNNEST(@languages)
      AND JSON_EXTRACT_SCALAR(p.payload, '$.pull_request.user.type') = 'User'
  ),
  per_repository_merges AS (
    SELECT
      repository,
      COUNT(DISTINCT change_number) AS total_merges
    FROM merges
    GROUP BY repository
  ),
  per_merge_review AS (
    SELECT
      m.repository,
      COUNTIF(
        EXISTS (
          SELECT 1
          FROM reviews r
          WHERE r.repository = m.repository
            AND r.change_number = m.change_number
            AND r.reviewer IS NOT NULL
            AND r.reviewer != m.change_author
            AND r.review_created_at <= m.merged_at
        )
      ) AS review_merges
    FROM merges m
    GROUP BY m.repository
  ),
  meta AS (
    SELECT
      i.repo.name AS repository,
      ANY_VALUE(JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.repo.license.key')) AS license,
      ANY_VALUE(JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.repo.language')) AS language,
      ANY_VALUE(JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.repo.created_at')) AS creation
    FROM `githubarchive.day.20*` AS i
    JOIN filtered f
      ON i.repo.name = f.repository
    WHERE
      i._TABLE_SUFFIX BETWEEN FORMAT_DATE('%y%m%d', DATE(@since))
                          AND FORMAT_DATE('%y%m%d', DATE(@until))
      AND i.type = 'PullRequestEvent'
      AND JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.user.type') = 'User'
      AND i.created_at >= @since
      AND i.created_at < @until
      AND JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.repo.language')
        IN UNNEST(@languages)
      AND JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.ref')
          = JSON_EXTRACT_SCALAR(i.payload, '$.pull_request.base.repo.default_branch')
    GROUP BY repository
  ),
  contributors AS (
    SELECT
      a.repository,
      COUNT(DISTINCT IF(r.user IS NOT NULL, a.user, NULL)) AS contributors,
      COUNT(DISTINCT IF(r.user IS NULL, a.user, NULL)) AS authors
    FROM (
      SELECT DISTINCT repository, change_author AS user
      FROM changes
    ) a
    LEFT JOIN (
      SELECT DISTINCT repository, reviewer AS user
      FROM reviews
      WHERE reviewer IS NOT NULL
    ) r
      ON a.repository = r.repository AND a.user = r.user
    GROUP BY a.repository
  )

SELECT
  f.repository AS name,
  f.total_changes,
  COALESCE(m.total_merges, 0) AS total_merges,
  COALESCE(c.contributors, 0) AS total_contributors,
  COALESCE(c.authors, 0) AS total_authors,
  f.total_reviews,
  f.total_reviewers,
  i.total_issues,
  r.total_resolutions,
  f.median_reviews_per_change,
  f.median_reviewers_per_change,
  f.review_changes,
  f.audit_changes,
  COALESCE(ms.review_merges, 0) AS review_merges,
  r.label_share,  
  d.default_target_share,
  l.license,
  l.language,
  l.creation,
  (SELECT window_days FROM time_window) AS window_days
FROM filtered f
LEFT JOIN per_repository_issues i USING (repository)
LEFT JOIN per_repository_resolutions r USING (repository)
LEFT JOIN per_repository_merges m USING (repository)
LEFT JOIN per_merge_review ms USING (repository)
LEFT JOIN meta l USING (repository)
LEFT JOIN contributors c USING (repository)
LEFT JOIN default_target d USING (repository)
WHERE i.total_issues >= 1 AND r.total_resolutions >= 1;