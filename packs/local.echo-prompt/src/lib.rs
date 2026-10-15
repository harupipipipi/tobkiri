//! Pack-owned prompt construction. The core knows none of these domain types.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use std::cell::RefCell;
use exports::local::echo_chat::prompt_builder::{Guest,Context,Input,Prompt};
use exports::tobkiri::core::lifecycle;
use tobkiri::core::types::{Error,ErrorCode};
use local::echo_chat::messages::Message;
#[derive(serde::Deserialize)]#[serde(deny_unknown_fields)]struct Settings{model:String,system_prompt:String,max_tokens:u32}
thread_local!{static SETTINGS:RefCell<Option<Settings>>=const{RefCell::new(None)};}
struct Pack;
impl lifecycle::Guest for Pack{
 fn init(config:String)->Result<(),Error>{let c:Settings=serde_json::from_str(&config).map_err(|_|Error{code:ErrorCode::Invalid,message:"prompt settings invalid".into()})?;
  if c.model.is_empty()||c.max_tokens==0||c.max_tokens>512{return Err(Error{code:ErrorCode::Invalid,message:"explicit model and bounded tokens required".into()});}
  SETTINGS.with_borrow_mut(|s|*s=Some(c));Ok(())}
 fn shutdown(){SETTINGS.with_borrow_mut(|s|*s=None);}
}
impl Guest for Pack{
 fn build(ctx:Context,input:Input)->Result<Prompt,Error>{SETTINGS.with_borrow(|s|{
  let c=s.as_ref().ok_or_else(||Error{code:ErrorCode::Invalid,message:"prompt not initialized".into()})?;
  if ctx.conversation!=input.conversation{return Err(Error{code:ErrorCode::Invalid,message:"conversation binding mismatch".into()});}
  let mut messages=vec![Message{role:"system".into(),content:c.system_prompt.clone()}];messages.extend(ctx.messages);
  messages.push(Message{role:"user".into(),content:input.text});Ok(Prompt{messages,model:c.model.clone(),max_tokens:c.max_tokens})})}
}
export!(Pack);
