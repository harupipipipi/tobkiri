//! Calendar conversion for the UTC strings persisted by the Python owners.

/// Convert Unix milliseconds to the original whole-second UTC format.
/// Return None for years beyond its four-digit calendar range.
pub fn utc_timestamp_from_millis(ms: u64) -> Option<String> {
    const SECONDS_PER_DAY: u64 = 86_400;
    const DAYS_PER_400_YEARS: u64 = 146_097;
    let seconds = ms / 1_000;
    // March-based Gregorian eras put each leap day at the end of its year.
    let days = seconds / SECONDS_PER_DAY + 719_468;
    let era = days / DAYS_PER_400_YEARS;
    let day_of_era = days % DAYS_PER_400_YEARS;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let march_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * march_month + 2) / 5 + 1;
    let month = (march_month + 2) % 12 + 1;
    let year = era * 400 + year_of_era + u64::from(month <= 2);
    if year > 9_999 {
        return None;
    }
    let hour = seconds % SECONDS_PER_DAY / 3_600;
    let minute = seconds % 3_600 / 60;
    let second = seconds % 60;
    Some(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}
