import type { Interval } from "../types/index.js";

/**
 * Utility class for half-open interval arithmetic.
 */
export class Intervals {
  /**
   * Returns a sorted, merged list with no adjacent or overlapping intervals.
   */
  static merge(list: Interval[]): Interval[] {
    if (list.length === 0) return [];
    const sorted = [...list].sort((first, second) => first[0] - second[0] || first[1] - second[1]);
    const result: Interval[] = [[sorted[0][0], sorted[0][1]]];
    for (let index = 1; index < sorted.length; index++) {
      const last = result[result.length - 1];
      const current = sorted[index];
      if (current[0] <= last[1]) {
        last[1] = Math.max(last[1], current[1]);
      } else {
        result.push([current[0], current[1]]);
      }
    }
    return result;
  }

  /**
   * Returns the merged union of two interval lists.
   */
  static union(first: Interval[], second: Interval[]): Interval[] {
    return Intervals.merge([...first, ...second]);
  }

  /**
   * Returns true if interval a and interval b share at least one point.
   */
  static overlaps(a: Interval, b: Interval): boolean;
  /**
   * Returns true if any interval in the list overlaps the target.
   */
  static overlaps(list: Interval[], target: Interval): boolean;
  static overlaps(a: Interval | Interval[], b: Interval): boolean {
    if (Array.isArray(a[0])) return (a as Interval[]).some((interval) => Intervals.overlaps(interval, b));
    const interval = a as Interval;
    return interval[0] < b[1] && b[0] < interval[1];
  }

  /**
   * Returns true if outer fully contains inner.
   */
  static contains(outer: Interval, inner: Interval): boolean;
  /**
   * Returns true if any interval in the list fully contains the target.
   */
  static contains(list: Interval[], target: Interval): boolean;
  static contains(outer: Interval | Interval[], inner: Interval): boolean {
    if (Array.isArray(outer[0])) return (outer as Interval[]).some((interval) => Intervals.contains(interval, inner));
    const interval = outer as Interval;
    return interval[0] <= inner[0] && interval[1] >= inner[1];
  }

  /**
   * Returns true if every interval in targets is fully contained by at least one interval in covering.
   */
  static covered(covering: Interval[], targets: Interval[]): boolean {
    return targets.every((target) => Intervals.contains(covering, target));
  }
}
