/** Great-circle distance in metres (haversine). */
export function distanceM(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const R = 6371000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1); const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

export interface GeoLocation { id: string; name: string; lat: number; lng: number; radius_m: number }

/** Nearest configured location and whether the point is inside its radius (accuracy widens the fence a little). */
export function geofence(lat: number, lng: number, accuracyM: number, locations: GeoLocation[]): { inside: boolean | null; locationId: string | null; distanceM: number | null; locationName: string | null } {
  if (locations.length === 0) return { inside: null, locationId: null, distanceM: null, locationName: null };
  let best: { loc: GeoLocation; d: number } | null = null;
  for (const loc of locations) {
    const d = distanceM(lat, lng, loc.lat, loc.lng);
    if (!best || d < best.d) best = { loc, d };
  }
  const tolerance = Math.min(accuracyM, 100);
  return { inside: best!.d <= best!.loc.radius_m + tolerance, locationId: best!.loc.id, distanceM: Math.round(best!.d), locationName: best!.loc.name };
}
