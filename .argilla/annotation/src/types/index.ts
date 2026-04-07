/**
 * Half-open line range [start, end) where start is inclusive and end is exclusive, 1-based.
 */
export type Interval = [number, number];

/**
 * The kind of change a file underwent in a commit.
 */
export type Action = "add" | "delete" | "change" | "unchanged";

/**
 * A file change entry with an explicit action.
 */
export interface Change {
  action: Action;
  source?: string;
  target?: string;
}

/**
 * A model-generated annotation suggestion attached to a target commit.
 */
export interface Annotation {
  /**
   * Repository-relative file path.
   */
  path: string;
  /**
   * Half-open line intervals the annotation covers.
   */
  lines: Interval[];
  /**
   * Human-readable description from the model.
   */
  description: string;
}

/**
 * A commit as represented in the input JSON.
 */
export interface Commit {
  /**
   * Full commit SHA.
   */
  sha: string;
  /**
   * Commit author name.
   */
  author: string;
  /**
   * Commit message.
   */
  message: string;
  /**
   * ISO 8601 timestamp.
   */
  time: string;
  /**
   * Classifier labels attached to the commit.
   */
  labels: string[];
  /**
   * File changes keyed by repository-relative path. A plain string value is an unchanged blob SHA.
   */
  changes: Record<string, string | Change>;
}

/**
 * A target commit that carries model-generated annotations.
 */
export interface Target extends Commit {
  /**
   * Model-generated annotation suggestions for this commit.
   */
  annotations: Annotation[];
}

/**
 * The top-level data structure loaded from the JSON URL.
 */
export interface Data {
  /**
   * Identifier of the model that produced the annotations.
   */
  model: string;
  /**
   * The context (base) commit shown read-only.
   */
  context: Commit;
  /**
   * Target commits the annotator reviews.
   */
  targets: Target[];
}

/**
 * The lifecycle status of a suggestion.
 */
export type Status = "pending" | "accepted" | "rejected";

/**
 * A model-generated suggestion in the selection state.
 */
export interface Suggestion {
  /**
   * Unique stable identifier for this suggestion.
   */
  identifier: string;
  /**
   * SHA of the commit this suggestion belongs to.
   */
  sha: string;
  /**
   * Repository-relative file path.
   */
  path: string;
  /**
   * Half-open line intervals covered by the suggestion.
   */
  lines: Interval[];
  /**
   * Human-readable description from the model.
   */
  description: string;
  /**
   * Current lifecycle status.
   */
  status: Status;
}

/**
 * An annotator-created manual selection.
 */
export interface Manual {
  /**
   * Unique stable identifier for this manual selection.
   */
  identifier: string;
  /**
   * SHA of the commit this selection belongs to.
   */
  sha: string;
  /**
   * Repository-relative file path.
   */
  path: string;
  /**
   * Half-open line intervals covered by the selection.
   */
  lines: Interval[];
}

/**
 * A single entry in the exported selection state (Section 10.1).
 */
export interface Entry {
  /**
   * SHA of the commit this entry belongs to.
   */
  sha: string;
  /**
   * Repository-relative file path.
   */
  path: string;
  /**
   * Merged half-open line intervals for this entry.
   */
  lines: Interval[];
}
