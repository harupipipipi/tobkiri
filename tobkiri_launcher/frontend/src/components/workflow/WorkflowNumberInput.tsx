import {useState} from 'react';

/** Canonical JSON numeric ports accept safe integers, never implicit decimals. */
export function WorkflowNumberInput({value,schema,disabled,className,onChange}:{value:unknown;schema:Record<string,unknown>;disabled:boolean;className:string;onChange:(value:number|undefined)=>void}) {
  const [error,setError]=useState('');
  return <><input type="number" className={className} disabled={disabled}
    value={typeof value==='number'?value:''} step={1}
    min={typeof schema.minimum==='number'?schema.minimum:undefined}
    max={typeof schema.maximum==='number'?schema.maximum:undefined}
    aria-invalid={error?true:undefined}
    onChange={event=>{
      const raw=event.target.value;
      if(raw===''){onChange(undefined);setError('');return;}
      const parsed=Number(raw);
      if(!Number.isSafeInteger(parsed)){
        onChange(undefined);
        setError('安全な整数を入力してください。小数の値はこの数値端子では扱えません');
        return;
      }
      onChange(parsed);setError('');
    }}/><span className="mt-1 block text-xs text-text-muted">この数値端子は安全な整数のみ対応</span>
    {error&&<span role="alert" className="block text-destructive">{error}</span>}</>;
}
