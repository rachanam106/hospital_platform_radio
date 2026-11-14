import {afterEach,describe,expect,it,vi} from 'vitest';
import {api,resetApiSession} from './client';
const response=(data:unknown,status=200)=>({ok:status<400,status,json:async()=>status<400?{data}:{error:{code:'permission_denied',message:'Not allowed'}}}) as Response;
describe('session API client',()=>{
 afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals();resetApiSession()});
 it('fetches CSRF before login and submits credentials without logging them',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({csrf_token:'csrf-1'})).mockResolvedValueOnce(response({user_id:7,csrf_token:'csrf-2'}));vi.stubGlobal('fetch',fetch);
  await api.login('staff','secret');
  expect(fetch).toHaveBeenNthCalledWith(1,'/api/v1/auth/csrf/',expect.objectContaining({credentials:'same-origin'}));
  expect(fetch.mock.calls[1][1]).toMatchObject({method:'POST',credentials:'same-origin',headers:expect.objectContaining({'X-CSRFToken':'csrf-1'})});
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({username:'staff',password:'secret'});
 });
 it('validates clinic selection through the active-context endpoint',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({csrf_token:'csrf-1'})).mockResolvedValueOnce(response({clinic:{id:'clinic-1'},roles:['technician','radiologist']}));vi.stubGlobal('fetch',fetch);
  const active=await api.activate('clinic-1');
  expect(active.roles).toEqual(['technician','radiologist']);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({clinic_id:'clinic-1'});
 });
 it('logs out with the current CSRF token and same-origin session cookie',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({csrf_token:'csrf-1'})).mockResolvedValueOnce(response({logged_out:true}));vi.stubGlobal('fetch',fetch);
  await api.csrf();await api.logout();
  expect(fetch.mock.calls[1][1]).toMatchObject({method:'POST',credentials:'same-origin',headers:expect.objectContaining({'X-CSRFToken':'csrf-1'})});
 });
 it('signals session expiry for an unauthenticated session response without clearing on permission denial',async()=>{
  const event=vi.fn();window.addEventListener('api-unauthorized',event);
  const fetch=vi.fn().mockResolvedValueOnce({ok:false,status:403,json:async()=>({error:{code:'not_authenticated',message:'Authentication required.'}})});vi.stubGlobal('fetch',fetch);
  await expect(api.get('/auth/me/')).rejects.toMatchObject({code:'not_authenticated'});expect(event).toHaveBeenCalledOnce();window.removeEventListener('api-unauthorized',event);
 });
 it('uploads files with CSRF and reports upload progress',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({csrf_token:'csrf-upload'}));vi.stubGlobal('fetch',fetch);const progress=vi.fn();let xhr:any;
  class MockXHR{upload:any={};status=200;responseText=JSON.stringify({data:[{duplicate:false}]});headers:Record<string,string>={};method='';url='';withCredentials=false;onload:(()=>void)|null=null;onerror:(()=>void)|null=null;body:FormData|null=null;constructor(){xhr=this}open(method:string,url:string){this.method=method;this.url=url}setRequestHeader(key:string,value:string){this.headers[key]=value}send(body:FormData){this.body=body;this.upload.onprogress?.({lengthComputable:true,loaded:50,total:100});this.onload?.()}}
  vi.stubGlobal('XMLHttpRequest',MockXHR);const result=await api.upload<{duplicate:boolean}[]>('/examinations/exam-1/imaging/',[new File(['synthetic'],'scan.png',{type:'image/png'})],progress);
  expect(result).toEqual([{duplicate:false}]);expect(fetch).toHaveBeenCalledWith('/api/v1/auth/csrf/',expect.objectContaining({credentials:'same-origin'}));expect(progress).toHaveBeenCalledWith(50);
  expect(xhr.method).toBe('POST');expect(xhr.url).toBe('/api/v1/examinations/exam-1/imaging/');expect(xhr.withCredentials).toBe(true);expect(xhr.headers['X-CSRFToken']).toBe('csrf-upload');expect(xhr.body?.getAll('files')).toHaveLength(1);
 });
 it('downloads signed PDFs through the same-origin authenticated session',async()=>{
  const blob=new Blob(['%PDF-synthetic'],{type:'application/pdf'});const fetch=vi.fn().mockResolvedValue({ok:true,status:200,blob:async()=>blob});vi.stubGlobal('fetch',fetch);
  await expect(api.download('/reports/report-1/pdf/')).resolves.toBe(blob);
  expect(fetch).toHaveBeenCalledWith('/api/v1/reports/report-1/pdf/',expect.objectContaining({headers:{Accept:'application/pdf, application/json'},credentials:'same-origin'}));
 });
});
