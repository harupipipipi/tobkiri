/* Internal SQLite numeric ABI v1. Never a Pack component or public WIT.
 * The host fixes the module, its capability broker, fuel and all capacities.
 * SQLite itself is the separately pinned public-domain amalgamation.
 */
#include "sqlite3.h"
#include <stdint.h>
#include <stddef.h>
#include <string.h>

__attribute__((import_module("tobkiri-sql"), import_name("broker")))
extern int64_t sql_broker(int op, int handle, int pointer, int length, int64_t arg);
__attribute__((import_module("tobkiri-sql"), import_name("check")))
extern int sql_check(void);

enum { OPEN=1, CLOSE, READ, WRITE, TRUNCATE, SIZE, SYNC, LOCK, REMOVE,
       EXISTS, SHM_LOCK };
enum { MAIN=0, JOURNAL=1, WAL=2, SHM=3, TEMP=4 };
#define HEAP_CAP (4u * 1024u * 1024u)
#define IO_CAP (64u * 1024u)
static unsigned char heap[HEAP_CAP] __attribute__((aligned(64)));
static sqlite3 *database;
static sqlite3_stmt *statement;
static int internal_transaction;
static int read_only;
static int64_t logical_now_ms;
static uint64_t random_state;
static uint64_t next_temporary=1;
static uint64_t next_statement=1;
static uint64_t statement_generation;
static uint32_t shm_calls;

typedef struct File {
  sqlite3_file base;
  int handle;
  int kind;
  uint64_t temporary;
  int delete_on_close;
} File;

static int io_result(int64_t value, int failure) {
  if (value >= 0) return SQLITE_OK;
  return value >= -65535 ? (int)-value : failure;
}
static int ready(void) { return sql_check() ? SQLITE_INTERRUPT : SQLITE_OK; }
static int kind_of(const char *name) {
  if (!name || !strcmp(name,"store")) return MAIN;
  if (!strcmp(name,"store-journal")) return JOURNAL;
  if (!strcmp(name,"store-wal")) return WAL;
  if (!strcmp(name,"store-shm")) return SHM;
  return -1;
}
static int close_file(sqlite3_file *base) {
  File *file=(File *)base;
  int result=SQLITE_OK;
  int closed=io_result(sql_broker(CLOSE,file->handle,0,0,0),SQLITE_IOERR_CLOSE);
  if (result==SQLITE_OK) result=closed;
  if (file->delete_on_close) {
    int removed=io_result(sql_broker(REMOVE,file->kind,0,0,(int64_t)file->temporary),SQLITE_IOERR_DELETE);
    if (result==SQLITE_OK) result=removed;
  }
  file->base.pMethods=0;
  return result;
}
static int read_file(sqlite3_file *base,void *out,int length,sqlite3_int64 offset) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  if (length<0 || length>IO_CAP || offset<0) return SQLITE_IOERR_READ;
  int64_t read=sql_broker(READ,((File *)base)->handle,(int)(uintptr_t)out,length,offset);
  if (read<0) return io_result(read,SQLITE_IOERR_READ);
  if (read>length) return SQLITE_IOERR_READ;
  if (read<length) { memset((unsigned char *)out+read,0,(size_t)(length-read)); return SQLITE_IOERR_SHORT_READ; }
  return SQLITE_OK;
}
static int write_file(sqlite3_file *base,const void *input,int length,sqlite3_int64 offset) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  if (length<0 || length>IO_CAP || offset<0) return SQLITE_IOERR_WRITE;
  return io_result(sql_broker(WRITE,((File *)base)->handle,(int)(uintptr_t)input,length,offset),SQLITE_IOERR_WRITE);
}
static int truncate_file(sqlite3_file *base,sqlite3_int64 size) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  if (size<0) return SQLITE_IOERR_TRUNCATE;
  return io_result(sql_broker(TRUNCATE,((File *)base)->handle,0,0,size),SQLITE_IOERR_TRUNCATE);
}
static int sync_file(sqlite3_file *base,int flags) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  return io_result(sql_broker(SYNC,((File *)base)->handle,0,0,flags),SQLITE_IOERR_FSYNC);
}
static int size_file(sqlite3_file *base,sqlite3_int64 *out) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  int64_t value=sql_broker(SIZE,((File *)base)->handle,0,0,0);
  if (value<0) return io_result(value,SQLITE_IOERR_FSTAT);
  *out=value; return SQLITE_OK;
}
static int lock_file(sqlite3_file *base,int level) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  return io_result(sql_broker(LOCK,((File *)base)->handle,0,0,level),SQLITE_IOERR_LOCK);
}
static int reserved_file(sqlite3_file *base,int *out) {
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  /* LOCK with -1 is a capability broker query, not an inferred successful lock. */
  int64_t value=sql_broker(LOCK,((File *)base)->handle,0,0,-1);
  if (value<0) return io_result(value,SQLITE_IOERR_CHECKRESERVEDLOCK);
  *out=value!=0; return SQLITE_OK;
}
static int control_file(sqlite3_file *base,int operation,void *argument) {
  (void)base; (void)operation; (void)argument; return SQLITE_NOTFOUND;
}
static int sector_file(sqlite3_file *base) { (void)base; return 4096; }
static int characteristics_file(sqlite3_file *base) { (void)base; return 0; }
/* Only one physical DATA DB connection is admitted by the host. Existing WAL
 * may use SQLite's private heap WAL-index in EXCLUSIVE locking mode. A copied
 * SHM cache cannot provide SQLite's coherent shared-memory semantics. Every
 * unexpected xShm call is therefore an explicit unsupported path. */
static int shm_map(sqlite3_file *base,int page,int size,int extend,void volatile **out) {
  (void)base; (void)page; (void)size; (void)extend;
  *out=0; shm_calls++; return SQLITE_IOERR_SHMMAP;
}
static void shm_barrier(sqlite3_file *base) {
  (void)base; shm_calls++; __builtin_trap();
}
static int shm_lock(sqlite3_file *base,int offset,int count,int flags) {
  (void)base; (void)offset; (void)count; (void)flags;
  shm_calls++; return SQLITE_IOERR_SHMLOCK;
}
static int shm_unmap(sqlite3_file *base,int remove) {
  (void)base; (void)remove; shm_calls++; return SQLITE_IOERR_SHMOPEN;
}
static const sqlite3_io_methods io_methods={2,close_file,read_file,write_file,truncate_file,
  sync_file,size_file,lock_file,lock_file,reserved_file,control_file,sector_file,
  characteristics_file,shm_map,shm_lock,shm_barrier,shm_unmap,0,0};
static int open_file(sqlite3_vfs *vfs,const char *name,sqlite3_file *base,int flags,int *actual) {
  (void)vfs;
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  File *file=(File *)base; memset(file,0,sizeof(*file));
  int kind=kind_of(name);
  if (!name && !(flags&SQLITE_OPEN_MAIN_DB)) { kind=TEMP; file->temporary=next_temporary++; }
  if (kind<0 || !next_temporary) return SQLITE_CANTOPEN;
  int64_t argument=((int64_t)file->temporary<<1)|((flags&SQLITE_OPEN_CREATE)!=0);
  int64_t id=sql_broker(OPEN,kind,0,0,argument);
  if (id<=0 || id>INT32_MAX) return io_result(id<0?id:-SQLITE_CANTOPEN,SQLITE_CANTOPEN);
  file->handle=(int)id; file->kind=kind;
  file->delete_on_close=(flags&SQLITE_OPEN_DELETEONCLOSE)!=0;
  file->base.pMethods=&io_methods; if (actual) *actual=flags; return SQLITE_OK;
}
static int remove_file(sqlite3_vfs *vfs,const char *name,int sync) {
  (void)vfs; (void)sync;
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  int kind=kind_of(name); if (kind<0) return SQLITE_CANTOPEN;
  return io_result(sql_broker(REMOVE,kind,0,0,0),SQLITE_IOERR_DELETE);
}
static int access_file(sqlite3_vfs *vfs,const char *name,int flags,int *out) {
  (void)vfs; (void)flags;
  if (ready()!=SQLITE_OK) return SQLITE_INTERRUPT;
  int kind=kind_of(name); if (kind<0) return SQLITE_CANTOPEN;
  int64_t value=sql_broker(EXISTS,kind,0,0,0);
  if (value<0) return io_result(value,SQLITE_IOERR_ACCESS);
  *out=value!=0; return SQLITE_OK;
}
static int full_path(sqlite3_vfs *vfs,const char *name,int length,char *out) {
  (void)vfs;
  if (kind_of(name)<0 || !name || (int)strlen(name)>=length) return SQLITE_CANTOPEN;
  strcpy(out,name); return SQLITE_OK;
}
static int randomness(sqlite3_vfs *vfs,int length,char *out) {
  (void)vfs;
  for (int i=0;i<length;i++) { random_state^=random_state<<13; random_state^=random_state>>7; random_state^=random_state<<17; out[i]=(char)random_state; }
  return length;
}
static int sleep_vfs(sqlite3_vfs *vfs,int microseconds) { (void)vfs; (void)microseconds; return 0; }
static int current_time(sqlite3_vfs *vfs,double *out) { (void)vfs; *out=2440587.5+(double)logical_now_ms/86400000.; return SQLITE_OK; }
static int current_time64(sqlite3_vfs *vfs,sqlite3_int64 *out) { (void)vfs; *out=INT64_C(210866760000000)+logical_now_ms; return SQLITE_OK; }
static sqlite3_vfs vfs={2,sizeof(File),32,0,"tobkiri-capability",0,open_file,remove_file,
  access_file,full_path,0,0,0,0,randomness,sleep_vfs,current_time,0,current_time64,0,0,0};
int sqlite3_os_init(void) { return sqlite3_vfs_register(&vfs,1); }
int sqlite3_os_end(void) { return SQLITE_OK; }

static int authorizer(void *ignored,int action,const char *a,const char *b,const char *db,const char *trigger) {
  (void)ignored; (void)db; (void)trigger;
  if (action==SQLITE_ATTACH || action==SQLITE_DETACH) return SQLITE_DENY;
  if ((action==SQLITE_TRANSACTION || action==SQLITE_SAVEPOINT) && !internal_transaction) return SQLITE_DENY;
  if (action==SQLITE_FUNCTION && b && (!sqlite3_stricmp(b,"load_extension") || !sqlite3_stricmp(b,"readfile") || !sqlite3_stricmp(b,"writefile") || !sqlite3_stricmp(b,"edit"))) return SQLITE_DENY;
  if (action==SQLITE_PRAGMA) {
    static const char *allowed[]={"table_info","table_xinfo","index_list","index_info","index_xinfo","foreign_key_list","user_version","application_id","integrity_check","quick_check"};
    int found=0; for (unsigned i=0;i<sizeof(allowed)/sizeof(allowed[0]);i++) if (a && !sqlite3_stricmp(a,allowed[i])) found=1;
    if (!found || (b && sqlite3_stricmp(a,"user_version") && sqlite3_stricmp(a,"application_id"))) return SQLITE_DENY;
  }
  return SQLITE_OK;
}
static int progress(void *unused) { (void)unused; return sql_check()!=0; }
int tobkiri_abi_version(void) { return 0x10000; }
int tobkiri_init(int heap_bytes,int64_t now_ms,int64_t seed) {
  if (heap_bytes<65536 || heap_bytes>HEAP_CAP) return SQLITE_MISUSE;
  logical_now_ms=now_ms; random_state=(uint64_t)seed|1;
  int result=sqlite3_config(SQLITE_CONFIG_HEAP,heap,heap_bytes,64);
  if (result!=SQLITE_OK) return result;
  result=sqlite3_config(SQLITE_CONFIG_MEMSTATUS,0);
  return result==SQLITE_OK ? sqlite3_initialize() : result;
}
void tobkiri_context(int64_t now_ms,int64_t seed) { logical_now_ms=now_ms; random_state=(uint64_t)seed|1; }
int tobkiri_alloc(int bytes) { return bytes<0?0:(int)(uintptr_t)sqlite3_malloc(bytes); }
void tobkiri_free(int pointer) { sqlite3_free((void *)(uintptr_t)(uint32_t)pointer); }
int tobkiri_open(int readonly) {
  if (database || statement) return SQLITE_MISUSE;
  read_only=readonly!=0;
  int flags=read_only?SQLITE_OPEN_READONLY:SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE;
  int result=sqlite3_open_v2("store",&database,flags,"tobkiri-capability");
  if (result!=SQLITE_OK) return result;
  /* This pragma does not require a schema read in the pinned SQLite source.
   * Set it before the first database read/recovery or guest prepare. The host
   * independently retains a whole physical DB connection lease. */
  result=sqlite3_exec(database,"PRAGMA locking_mode=EXCLUSIVE",0,0,0);
  if (result!=SQLITE_OK) return result;
  sqlite3_extended_result_codes(database,1);
  sqlite3_db_config(database,SQLITE_DBCONFIG_DEFENSIVE,1,0);
  sqlite3_db_config(database,SQLITE_DBCONFIG_TRUSTED_SCHEMA,0,0);
  sqlite3_db_config(database,SQLITE_DBCONFIG_LOOKASIDE,0,0,0);
  sqlite3_limit(database,SQLITE_LIMIT_LENGTH,1024*1024);
  sqlite3_limit(database,SQLITE_LIMIT_SQL_LENGTH,256*1024);
  sqlite3_limit(database,SQLITE_LIMIT_COLUMN,64);
  sqlite3_limit(database,SQLITE_LIMIT_VARIABLE_NUMBER,256);
  sqlite3_set_authorizer(database,authorizer,0);
  sqlite3_progress_handler(database,1000,progress,0);
  return SQLITE_OK;
}
int tobkiri_close(void) {
  if (statement) { sqlite3_finalize(statement); statement=0; }
  int result=database?sqlite3_close(database):SQLITE_OK;
  if (result==SQLITE_OK) database=0; return result;
}
int tobkiri_prepare(int pointer,int length,int parameters,int readonly_query) {
  if (!database || statement || length<=0 || length>256*1024) return SQLITE_MISUSE;
  const char *input=(const char *)(uintptr_t)(uint32_t)pointer;
  if (memchr(input,0,(size_t)length)) return SQLITE_MISUSE;
  const char *tail=0;
  int result=sqlite3_prepare(database,input,length,&statement,&tail);
  if (result!=SQLITE_OK) return result;
  if (!statement || sqlite3_bind_parameter_count(statement)!=parameters) goto invalid;
  /* Preparing a second statement cannot execute it; authorizer still applies. */
  while (tail<input+length) {
    sqlite3_stmt *extra=0; const char *next=0;
    result=sqlite3_prepare(database,tail,(int)(input+length-tail),&extra,&next);
    if (extra) { sqlite3_finalize(extra); goto invalid; }
    if (result!=SQLITE_OK || !next || next<=tail) goto invalid;
    tail=next;
  }
  for (int i=1;i<=parameters;i++) {
    const char *name=sqlite3_bind_parameter_name(statement,i);
    if (name && name[0]!='?') goto invalid;
  }
  if ((readonly_query || read_only) && !sqlite3_stmt_readonly(statement)) goto invalid;
  if (!next_statement) goto invalid;
  statement_generation=next_statement++;
  return SQLITE_OK;
invalid:
  sqlite3_finalize(statement); statement=0; statement_generation=0; return SQLITE_AUTH;
}
int64_t tobkiri_stmt_generation(void) { return (int64_t)statement_generation; }
int tobkiri_shm_calls(void) { return (int)shm_calls; }
int tobkiri_stmt_readonly(void) { return statement?sqlite3_stmt_readonly(statement):0; }
int tobkiri_bind_null(int i) { return sqlite3_bind_null(statement,i); }
int tobkiri_bind_integer(int i,int64_t value) { return sqlite3_bind_int64(statement,i,value); }
int tobkiri_bind_real(int i,double value) { return sqlite3_bind_double(statement,i,value); }
int tobkiri_bind_text(int i,int p,int n) { return sqlite3_bind_text(statement,i,(const char *)(uintptr_t)(uint32_t)p,n,SQLITE_TRANSIENT); }
int tobkiri_bind_blob(int i,int p,int n) { return sqlite3_bind_blob(statement,i,(const void *)(uintptr_t)(uint32_t)p,n,SQLITE_TRANSIENT); }
int tobkiri_step(void) {
  if (!statement || !statement_generation) return SQLITE_MISUSE;
  int result=sqlite3_step(statement);
  /* Legacy prepare never transparently recompiles+steps. Reset only obtains
   * its specific error; the host must finish and explicitly reprepare under a
   * fresh classification barrier, and must not retry unknown mutations. */
  if (result==SQLITE_ERROR) result=sqlite3_reset(statement);
  if (result!=SQLITE_ROW) statement_generation=0;
  return result;
}
int tobkiri_column_count(void) { return statement?sqlite3_column_count(statement):0; }
int tobkiri_column_name(int i) { return (int)(uintptr_t)sqlite3_column_name(statement,i); }
int tobkiri_column_type(int i) { return sqlite3_column_type(statement,i); }
int tobkiri_column_bytes(int i) { return sqlite3_column_bytes(statement,i); }
int tobkiri_column_text(int i) { return (int)(uintptr_t)sqlite3_column_text(statement,i); }
int tobkiri_column_blob(int i) { return (int)(uintptr_t)sqlite3_column_blob(statement,i); }
int64_t tobkiri_column_integer(int i) { return sqlite3_column_int64(statement,i); }
double tobkiri_column_real(int i) { return sqlite3_column_double(statement,i); }
int64_t tobkiri_changes(void) { return sqlite3_changes64(database); }
int64_t tobkiri_last_rowid(void) { return sqlite3_last_insert_rowid(database); }
int tobkiri_finish(void) { int result=statement?sqlite3_finalize(statement):SQLITE_OK; statement=0; statement_generation=0; return result; }
int tobkiri_autocommit(void) { return database?sqlite3_get_autocommit(database):1; }
int tobkiri_control(int operation) {
  static const char *commands[]={"BEGIN DEFERRED","COMMIT","ROLLBACK"};
  if (!database || statement || operation<0 || operation>2) return SQLITE_MISUSE;
  internal_transaction=1;
  int result=sqlite3_exec(database,commands[operation],0,0,0);
  internal_transaction=0; return result;
}
int tobkiri_error_code(void) { return database?sqlite3_extended_errcode(database):SQLITE_MISUSE; }
/* Turn any libc abort into a bounded Wasm trap; never import WASI proc_exit. */
__attribute__((noreturn)) void abort(void) { __builtin_trap(); }
