/** Pure cache-version selection used by BinaryResolver and regression tests. */
export function shouldScanFallbackCacheVersions(allowLatestFallback: boolean): boolean {
  return allowLatestFallback;
}

export function matchesRequestedCacheVersion(cachedVersion: string, requestedVersion: string): boolean {
  return cachedVersion === requestedVersion;
}
