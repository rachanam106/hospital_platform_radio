import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import SettingsWorkspace from './SettingsWorkspace';

afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()});

it('shows honest security and unsupported preference status in settings',async()=>{
 const fetch=vi.fn(async(url:string)=>({ok:true,status:200,json:async()=>({data:url.includes('/clinics/profile/')?{name:'North Clinic',timezone:'Asia/Kolkata',default_currency:'INR',invoice_prefix:''}:[]})}));
 vi.stubGlobal('fetch',fetch);
 render(<SettingsWorkspace roles={['owner']} notify={vi.fn()}/>);
 fireEvent.click(screen.getByRole('tab',{name:'Security'}));
 expect(await screen.findByText(/MFA, device\/session inventory and password recovery workflows are not implemented/)).toBeInTheDocument();
 fireEvent.click(screen.getByRole('tab',{name:'Preferences'}));
 expect(screen.getByText(/timezone.*not currently honored/i)).toBeInTheDocument();
 await waitFor(()=>expect(fetch).toHaveBeenCalled());
});

it('keeps owner transfer controls out of administrator settings',async()=>{
 const fetch=vi.fn(async(url:string)=>({ok:true,status:200,json:async()=>({data:url.includes('/memberships/')?{count:1,results:[{id:'m1',user_id:3,roles:['admin'],is_active:true}]}:[]})}));
 vi.stubGlobal('fetch',fetch);
 render(<SettingsWorkspace roles={['admin']} notify={vi.fn()}/>);
 fireEvent.click(screen.getByRole('tab',{name:'Staff & access'}));
 expect(await screen.findByText('Account 3')).toBeInTheDocument();
 expect(screen.queryByText('Transfer ownership')).not.toBeInTheDocument();
});

it('creates a staff account and displays its temporary password once',async()=>{
 const calls:Array<{url:string;method:string;body:any}> = [];
 const fetch=vi.fn(async(url:string,init?:RequestInit)=>{const method=init?.method??'GET';const body=init?.body?JSON.parse(String(init.body)):null;calls.push({url,method,body});
  if(url.includes('/memberships/provision/'))return {ok:true,status:201,json:async()=>({data:{membership:{id:'m2'},temporary_password:'one-time-secret'}})};
  if(url.includes('/memberships/'))return {ok:true,status:200,json:async()=>({data:[{id:'m1',user_id:1,username:'new-user',email:'new@example.invalid',full_name:'New Staff',roles:['technician'],is_active:true}]})};
  return {ok:true,status:200,json:async()=>({data:{name:'Clinic',timezone:'Asia/Kolkata'}})};
 });vi.stubGlobal('fetch',fetch);
 render(<SettingsWorkspace roles={['owner']} notify={vi.fn()}/>);fireEvent.click(screen.getByRole('tab',{name:'Staff & access'}));
 fireEvent.click(await screen.findByRole('button',{name:'Add staff member'}));
 fireEvent.change(screen.getByLabelText('Full name'),{target:{value:'New Staff'}});fireEvent.change(screen.getByLabelText('Email address'),{target:{value:'new@example.invalid'}});fireEvent.change(screen.getByLabelText('Username'),{target:{value:'new-user'}});fireEvent.click(screen.getByLabelText('technician'));
 fireEvent.click(screen.getByRole('button',{name:'Create staff account'}));
 expect(await screen.findByText('one-time-secret')).toBeInTheDocument();expect(screen.getByText('New Staff')).toBeInTheDocument();
 expect(calls.find(x=>x.url.includes('/memberships/provision/'))?.body).toMatchObject({username:'new-user',roles:['technician']});
 fireEvent.click(screen.getByRole('button',{name:'I have saved it securely'}));expect(screen.queryByText('one-time-secret')).not.toBeInTheDocument();
});

it('sends role changes and confirmed membership deactivation to the backend',async()=>{
 const calls:Array<{url:string;method:string;body:any}> = [];
 const fetch=vi.fn(async(url:string,init?:RequestInit)=>{const method=init?.method??'GET';const body=init?.body?JSON.parse(String(init.body)):null;calls.push({url,method,body});
  if(url.includes('/memberships/m1/roles/'))return {ok:true,status:201,json:async()=>({data:{membership_id:'m1',role:'radiologist',is_active:true}})};
  if(url.includes('/memberships/m1/active/'))return {ok:true,status:200,json:async()=>({data:{id:'m1',is_active:false}})};
  if(url.includes('/memberships/'))return {ok:true,status:200,json:async()=>({data:[{id:'m1',user_id:1,username:'staff-one',email:'staff@example.invalid',full_name:'Staff One',roles:['technician'],is_active:true}]})};
  return {ok:true,status:200,json:async()=>({data:{name:'Clinic',timezone:'Asia/Kolkata'}})};
 });vi.stubGlobal('fetch',fetch);vi.stubGlobal('confirm',vi.fn(()=>true));
 render(<SettingsWorkspace roles={['admin']} notify={vi.fn()}/>);fireEvent.click(screen.getByRole('tab',{name:'Staff & access'}));await screen.findByText('Staff One');
 fireEvent.change(screen.getByRole('combobox',{name:'Add role staff-one'}),{target:{value:'radiologist'}});
 await waitFor(()=>expect(calls.some(c=>c.url.includes('/roles/')&&c.method==='POST'&&c.body.role==='radiologist')).toBe(true));
 fireEvent.change(screen.getByRole('combobox',{name:'Membership status staff-one'}),{target:{value:'false'}});
 await waitFor(()=>expect(calls.some(c=>c.url.includes('/active/')&&c.method==='PATCH'&&c.body.is_active===false)).toBe(true));
 expect(window.confirm).toHaveBeenCalled();
});
