//! Conversation persistence using only the Pack's private core store.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use exports::local::echo_chat::conversation_store::{Guest,Context,Input,ChatEvent};
use local::echo_chat::messages::Message;
use exports::tobkiri::core::lifecycle;
use tobkiri::core::{storage,types::{Error,ErrorCode}};
struct Pack;
fn initialize()->Result<storage::Store,Error>{let store=storage::open("conversations")?;
 store.sql_exec("CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)",&[])?;Ok(store)}
impl lifecycle::Guest for Pack{
 fn init(_config:String)->Result<(),Error>{initialize()?;Ok(())}
 fn shutdown(){}
}
impl Guest for Pack{
 fn load(conversation:String)->Result<Context,Error>{let store=initialize()?;
  let rows=store.sql_query("SELECT role, content FROM messages WHERE conversation = ? ORDER BY seq",&[serde_json::to_string(&conversation).unwrap()],1000)?;
  let mut messages=Vec::new();for row in rows{if row.len()!=2{return Err(Error{code:ErrorCode::Internal,message:"invalid stored message".into()});}
   messages.push(Message{role:serde_json::from_str(&row[0]).map_err(|_|Error{code:ErrorCode::Invalid,message:"invalid role".into()})?,content:serde_json::from_str(&row[1]).map_err(|_|Error{code:ErrorCode::Invalid,message:"invalid content".into()})?});}
  Ok(Context{conversation,messages})}
 fn append(conversation:String,input:Input,events:Vec<ChatEvent>)->Result<(),Error>{
  if input.conversation!=conversation{return Err(Error{code:ErrorCode::Invalid,message:"conversation binding mismatch".into()});}
  let completed=events.iter().rev().find(|e|e.kind=="done").and_then(|e|e.text.clone()).ok_or_else(||Error{code:ErrorCode::Invalid,message:"incomplete stream cannot be saved as a turn".into()})?;
  let store=initialize()?;
  // One atomic statement preserves both messages or neither.
  store.sql_exec("INSERT INTO messages (conversation, role, content) VALUES (?, 'user', ?), (?, 'assistant', ?)",&[serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&input.text).unwrap(),serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&completed).unwrap()])?;Ok(())}
}
export!(Pack);
