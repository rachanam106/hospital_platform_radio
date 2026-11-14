import {afterEach,expect,it,vi} from 'vitest';import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';import App from './App';
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()});
function sessionResponse(roles:string[]){const clinic={id:'clinic-a',name:'North Imaging',timezone:'Asia/Kolkata'};return {data:{user_id:11,active_clinic_id:clinic.id,memberships:[{id:'membership-a',clinic,roles,is_active:true}]}}}
it('combines navigation for a user with technician and radiologist roles',async()=>{
 const fetch=vi.fn(async(url:string)=>({ok:true,status:200,json:async()=>url.endsWith('/auth/me/')?sessionResponse(['technician','radiologist']):{data:{count:0,next:null,previous:null,results:[]}}}));vi.stubGlobal('fetch',fetch);
 render(<App/>);await waitFor(()=>expect(screen.getByText('Examination queue')).toBeInTheDocument());
 expect(screen.getByText('Reporting')).toBeInTheDocument();expect(screen.getByText('Patients')).toBeInTheDocument();expect(screen.queryByText('Billing')).not.toBeInTheDocument();
});
it('shows reception and billing navigation for a receptionist',async()=>{
 const fetch=vi.fn(async(url:string)=>({ok:true,status:200,json:async()=>url.endsWith('/auth/me/')?sessionResponse(['receptionist']):{data:{count:0,next:null,previous:null,results:[]}}}));vi.stubGlobal('fetch',fetch);
 render(<App/>);await waitFor(()=>expect(screen.getAllByText('Appointments').length).toBeGreaterThan(0));expect(screen.getByText('Billing')).toBeInTheDocument();expect(screen.queryByText('Reporting')).not.toBeInTheDocument();
});
it('gates a first-login staff session until password change succeeds',async()=>{
 let mustChange=true;const fetch=vi.fn(async(url:string)=>{if(url.endsWith('/auth/me/')){const result=sessionResponse(['receptionist']);(result.data as any).must_change_password=mustChange;return {ok:true,status:200,json:async()=>result}}if(url.endsWith('/auth/password/change/')){mustChange=false;return {ok:true,status:200,json:async()=>({data:{password_changed:true,csrf_token:'fresh-csrf'}})}}return {ok:true,status:200,json:async()=>({data:[]})}});vi.stubGlobal('fetch',fetch);
 render(<App/>);expect(await screen.findByRole('heading',{name:'Choose a new password'})).toBeInTheDocument();expect(screen.queryByText('Patients')).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText('Temporary password'),{target:{value:'temporary-password'}});fireEvent.change(screen.getByLabelText('New password'),{target:{value:'A-new-password-123!'}});fireEvent.change(screen.getByLabelText('Confirm new password'),{target:{value:'A-new-password-123!'}});fireEvent.click(screen.getByRole('button',{name:'Change password'}));
 await waitFor(()=>expect(screen.getByText('Patients')).toBeInTheDocument());expect(fetch).toHaveBeenCalledWith('/api/v1/auth/password/change/',expect.objectContaining({method:'POST',credentials:'same-origin'}));
});
it('sends patient search and page changes to the backend API',async()=>{
 const requests:string[]=[];const person=(id:string,name:string)=>({id,medical_record_number:`MR-${id}`,given_name:name,family_name:'Example',date_of_birth:null,sex:'',phone:'',email:'',created_at:'2026-01-01T00:00:00Z'});
 const fetch=vi.fn(async(url:string)=>{requests.push(url);const parsed=new URL(url,'http://clinic.test');if(parsed.pathname==='/api/v1/auth/me/')return {ok:true,status:200,json:async()=>sessionResponse(['receptionist'])};if(parsed.pathname==='/api/v1/patients/')return {ok:true,status:200,json:async()=>({data:{count:26,next:null,previous:null,results:[person(parsed.searchParams.get('page')==='2'?'second':'first',parsed.searchParams.get('page')==='2'?'June':'Mira')]}})};return {ok:true,status:200,json:async()=>({data:{count:0,next:null,previous:null,results:[]}})}});vi.stubGlobal('fetch',fetch);
 render(<App/>);await screen.findByRole('button',{name:'Patients'});fireEvent.click(screen.getByRole('button',{name:'Patients'}));const search=await screen.findByRole('textbox',{name:'Search patients'});fireEvent.change(search,{target:{value:'Mira Rao'}});
 await waitFor(()=>expect(requests.some(url=>url.includes('/patients/?')&&url.includes('search=Mira+Rao'))).toBe(true));expect(await screen.findByText('Mira Example')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Next'}));await waitFor(()=>expect(requests.some(url=>url.includes('/patients/?')&&url.includes('page=2'))).toBe(true));expect(await screen.findByText('June Example')).toBeInTheDocument();
});
