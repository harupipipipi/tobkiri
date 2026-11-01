//! Secure storage is explicitly unavailable where the capability VFS is not implemented.
pub(crate) struct DatabaseGuard;
impl DatabaseGuard {
    pub(crate) fn quarantine(&self) {}
    pub(crate) fn is_quarantined(&self) -> bool {
        true
    }
    pub(crate) fn retain_connection(self, _connection: rusqlite::Connection) {
        unreachable!("native DATA connections are unavailable on this platform")
    }
}
