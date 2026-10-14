/** Match the Host's conservative canonical JSON subset before any journal/write. */
export function assertWorkflowCanonicalInput(value:unknown,maxBytes=3*1024*1024):void {
  const visit=(item:unknown,depth:number):void=>{
    if(depth>48)throw new Error('Flowの入力が深すぎます');
    if(typeof item==='number'&&!Number.isSafeInteger(item))throw new Error('数値は安全な整数で指定してください。小数は、そのノードが明示的に対応する場合だけ、小数文字列や単位付き整数で指定できます');
    if(typeof item==='string'){
      for(const char of item){const code=char.codePointAt(0)!;if(code>=0xd800&&code<=0xdfff)throw new Error('入力に不正なUnicode文字が含まれています');}
    }else if(Array.isArray(item)){for(const child of item)visit(child,depth+1);}
    else if(item!==null&&typeof item==='object'){for(const [key,child] of Object.entries(item)){visit(key,depth);visit(child,depth+1);}}
    else if(item!==null&&!['boolean','number','string'].includes(typeof item))throw new Error('Flowの入力はJSON値で指定してください');
  };
  visit(value,0);
  if(new TextEncoder().encode(JSON.stringify(value)).byteLength>maxBytes)throw new Error('Flowの入力が大きすぎます。内容を小さくしてから実行してください');
}
