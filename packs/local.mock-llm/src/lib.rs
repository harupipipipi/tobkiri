//! Deterministic streaming provider, independent of host/provider selection.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use std::{cell::RefCell,collections::{BTreeMap,VecDeque}};
use exports::local::harness_chat::llm_provider::{Guest,Prompt};
use exports::tobkiri::core::{lifecycle,stream_source};
use tobkiri::core::{flow,types::{Error,ErrorCode,StreamHandle}};
thread_local! {static STREAMS:RefCell<(u64,BTreeMap<u64,VecDeque<String>>)>=RefCell::new((0,BTreeMap::new()));}
struct Pack;
impl lifecycle::Guest for Pack {
 fn init(_config:String)->Result<(),Error>{STREAMS.with_borrow_mut(|s|s.1.clear());Ok(())}
 fn shutdown(){STREAMS.with_borrow_mut(|s|s.1.clear());}
}
impl Guest for Pack {
 fn chat(prompt:Prompt)->Result<StreamHandle,Error>{
  if flow::is_cancelled(){return Err(Error{code:ErrorCode::Cancelled,message:"run cancelled".into()});}
  let input=prompt.messages.iter().rev().find(|m|m.role=="user").map(|m|m.content.as_str()).unwrap_or("");
  let output=format!("Echo: {input}");
  let mut events=output.chars().map(|c|tobkiri_guest_sdk::delta_event(&c.to_string())).collect::<VecDeque<_>>();
  events.push_back(tobkiri_guest_sdk::done_event(&output));
  STREAMS.with_borrow_mut(|s|{s.0+=1;let id=s.0;s.1.insert(id,events);Ok(StreamHandle{id})})
 }
}
impl stream_source::Guest for Pack {
 fn pull(id:u64,max:u32)->Result<Vec<String>,Error>{
  if max==0||max>64{return Err(Error{code:ErrorCode::Invalid,message:"pull batch must be 1..64".into()});}
  if flow::is_cancelled(){Self::cancel(id);return Err(Error{code:ErrorCode::Cancelled,message:"run cancelled".into()});}
  STREAMS.with_borrow_mut(|s|{let q=s.1.get_mut(&id).ok_or_else(||Error{code:ErrorCode::NotFound,message:"stream not found".into()})?;
   let result=(0..max).filter_map(|_|q.pop_front()).collect::<Vec<_>>();
   if result.is_empty(){s.1.remove(&id);}Ok(result)})
 }
 fn cancel(id:u64){STREAMS.with_borrow_mut(|s|{s.1.remove(&id);});}
}
export!(Pack);
