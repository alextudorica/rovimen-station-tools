/*
 * dashboard-sky.js
 * ────────────────
 * Small, pure sky calculations for the Showers page: the Sun's ecliptic
 * longitude (to turn a shower's solar longitude into this year's date), the
 * Moon's illuminated fraction, and the altitude of a radiant or the Sun from
 * a site. Low-precision formulas from Meeus, "Astronomical Algorithms"
 * (ch. 25 Sun, ch. 48 Moon phase, ch. 12 sidereal time): the Sun to about
 * 0.01 deg (minutes of time), the Moon's lit fraction to about 1 %, which is
 * far below what a shower calendar needs. No DOM, no clock: every function
 * takes its time as an argument.
 */

const RAD = Math.PI / 180;
const wrap360 = x => ((x % 360) + 360) % 360;
const wrap180 = x => wrap360(x + 180) - 180;

/** Julian Date of a JS Date (UTC). */
export function jdFromDate(d) {
  return d.getTime() / 86400000 + 2440587.5;
}

/** JS Date of a Julian Date. */
export function dateFromJd(jd) {
  return new Date((jd - 2440587.5) * 86400000);
}

/** The Sun's geometric ecliptic longitude: of date, and on the J2000
 *  equinox (what the IAU Meteor Data Center and GMN give showers in), plus
 *  its right ascension and declination of date, in degrees. */
export function sun(jd) {
  const T = (jd - 2451545.0) / 36525;
  const L0 = wrap360(280.46646 + 36000.76983 * T + 0.0003032 * T * T);
  const M = wrap360(357.52911 + 35999.05029 * T - 0.0001537 * T * T);
  const C = (1.914602 - 0.004817 * T) * Math.sin(M * RAD)
          + (0.019993 - 0.000101 * T) * Math.sin(2 * M * RAD)
          + 0.000289 * Math.sin(3 * M * RAD);
  const lon = wrap360(L0 + C);
  // Precession in longitude from the J2000 equinox (about 50.3" a year).
  const lon2000 = wrap360(lon - 1.396971 * T);
  const eps = 23.439291 - 0.0130042 * T;
  const ra = wrap360(Math.atan2(Math.cos(eps * RAD) * Math.sin(lon * RAD), Math.cos(lon * RAD)) / RAD);
  const dec = Math.asin(Math.sin(eps * RAD) * Math.sin(lon * RAD)) / RAD;
  return { lon, lon2000, ra, dec };
}

/** The UTC instant in `year` at which the Sun reaches J2000 solar longitude
 *  `lon2000` (degrees). A shower's peak or activity limit in that year. */
export function solToDate(lon2000, year) {
  // Start from the mean rate (the March equinox is near 20 March, day 79)
  // and refine: the Sun's speed varies by about 3 % over the year.
  const jan1 = jdFromDate(new Date(Date.UTC(year, 0, 1)));
  let jd = jan1 + 78.8 + wrap360(lon2000) / 360 * 365.2422;
  // Longitudes after about 280 deg (January to mid-March) belong to the
  // start of the calendar year, not the end.
  if (jd >= jan1 + 365) jd -= 365.2422;
  for (let i = 0; i < 4; i++) jd -= wrap180(sun(jd).lon2000 - lon2000) / 0.98565;
  return dateFromJd(jd);
}

/** Day of year (0 = 1 Jan 00:00 UTC, fractional) of a Date. */
export function dayOfYear(d) {
  return (d.getTime() - Date.UTC(d.getUTCFullYear(), 0, 1)) / 86400000;
}

/** The Moon's illuminated fraction (0..1) and whether it is waxing. */
export function moonPhase(jd) {
  const T = (jd - 2451545.0) / 36525;
  const D = wrap360(297.8501921 + 445267.1114034 * T);   // mean elongation
  const M = wrap360(357.5291092 + 35999.0502909 * T);    // Sun's mean anomaly
  const Mp = wrap360(134.9633964 + 477198.8675055 * T);  // Moon's mean anomaly
  const i = 180 - D
    - 6.289 * Math.sin(Mp * RAD) + 2.100 * Math.sin(M * RAD)
    - 1.274 * Math.sin((2 * D - Mp) * RAD) - 0.658 * Math.sin(2 * D * RAD)
    - 0.214 * Math.sin(2 * Mp * RAD) - 0.110 * Math.sin(D * RAD);
  return { fraction: (1 + Math.cos(i * RAD)) / 2, waxing: D < 180 };
}

/** "Waxing gibbous" and friends, from moonPhase(). */
export function moonPhaseName({ fraction, waxing }) {
  if (fraction < 0.03) return 'New Moon';
  if (fraction > 0.97) return 'Full Moon';
  const side = waxing ? 'waxing' : 'waning';
  if (fraction < 0.4) return `${side} crescent`;
  if (fraction <= 0.6) return waxing ? 'first quarter' : 'last quarter';
  return `${side} gibbous`;
}

/** Greenwich mean sidereal time, degrees. */
function gmst(jd) {
  const T = (jd - 2451545.0) / 36525;
  return wrap360(280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T);
}

/** Altitude (degrees) of RA/Dec (degrees) from latitude/longitude (east +). */
export function altitude(ra, dec, jd, lat, lon) {
  const H = (gmst(jd) + lon - ra) * RAD;
  return Math.asin(Math.sin(lat * RAD) * Math.sin(dec * RAD)
                 + Math.cos(lat * RAD) * Math.cos(dec * RAD) * Math.cos(H)) / RAD;
}

/**
 * The night around `when` seen from `site`, in 10-minute steps from the noon
 * before to the noon after (UTC noon - site longitude, i.e. local solar noon).
 * Returns the dark interval (Sun below `darkAlt`), and when a radiant at
 * RA/Dec is at least `minAlt` up in the dark, with its highest point then.
 * `ra`/`dec` are J2000; precession to the date (under 0.5 deg) is ignored.
 */
export function nightView(ra, dec, when, site, { darkAlt = -12, minAlt = 25 } = {}) {
  const step = 10 / 1440;
  // Local solar noon before `when` starts the night that contains it.
  const noonOffset = -site.lon / 360;
  const jdWhen = jdFromDate(when);
  let start = Math.floor(jdWhen - 0.5 - noonOffset) + 0.5 + noonOffset + 0.5;
  if (start > jdWhen) start -= 1;
  let dark = null, good = null, best = null;
  for (let jd = start; jd <= start + 1; jd += step) {
    const s = sun(jd);
    const sunAlt = altitude(s.ra, s.dec, jd, site.lat, site.lon);
    if (sunAlt > darkAlt) continue;
    dark = dark ? { from: dark.from, to: jd } : { from: jd, to: jd };
    const alt = altitude(ra, dec, jd, site.lat, site.lon);
    if (!best || alt > best.alt) best = { jd, alt };
    if (alt >= minAlt) good = good ? { from: good.from, to: jd } : { from: jd, to: jd };
  }
  const d = x => x && { from: dateFromJd(x.from), to: dateFromJd(x.to) };
  return { dark: d(dark), good: d(good), best: best && { at: dateFromJd(best.jd), alt: best.alt } };
}
