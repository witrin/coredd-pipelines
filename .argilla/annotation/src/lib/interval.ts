/**
 * Half-open line range [start, end) where start is inclusive and end is exclusive, 1-based.
 */
export type Interval = [number, number];

/**
 * Utility class for half-open interval arithmetic.
 */
export class Intervals {
  /**
   * Returns true if two intervals share at least one interior point.
   */
  static overlaps(a: Interval, b: Interval): boolean {
    return a[0] < b[1] && b[0] < a[1];
  }

  /**
   * Returns true if outer fully contains inner.
   */
  static contains(outer: Interval, inner: Interval): boolean {
    return outer[0] <= inner[0] && outer[1] >= inner[1];
  }

  /**
   * Returns true if two intervals overlap or meet at a shared boundary.
   */
  static adjacent(a: Interval, b: Interval): boolean {
    return Intervals.overlaps(a, b) || a[1] === b[0] || b[1] === a[0];
  }

  /**
   * Returns the contiguous union of two intervals that overlap or touch.
   * The caller guarantees adjacency; the lower start and higher end define the range.
   */
  static join(a: Interval, b: Interval): Interval {
    return [Math.min(a[0], b[0]), Math.max(a[1], b[1])];
  }
}
