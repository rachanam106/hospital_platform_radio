export type Role = 'owner'|'admin'|'receptionist'|'technician'|'radiologist';
export interface Clinic { id:string; name:string; timezone:string }
export interface Membership { id:string; clinic:Clinic; roles:Role[]; is_active:boolean }
export interface SessionInfo { user_id:number; username:string; full_name:string; must_change_password:boolean; memberships:Membership[]; active_clinic_id:string|null }
export interface Page<T> { count:number; next:string|null; previous:string|null; results:T[] }
export class ApiError extends Error { constructor(public status:number, public code:string, message:string, public details?:unknown){super(message);this.name='ApiError'} }
let csrf:string|undefined;
export function rememberCsrf(value:string){ csrf=value }
export function resetApiSession(){ csrf=undefined }
async function request<T>(path:string, method='GET', body?:unknown):Promise<T>{
  const headers:Record<string,string>={Accept:'application/json'};
  if(body!==undefined) headers['Content-Type']='application/json';
  if(['POST','PATCH','PUT','DELETE'].includes(method)){
    if(!csrf) { const token=await request<{csrf_token:string}>('/auth/csrf/'); csrf=token.csrf_token; }
    headers['X-CSRFToken']=csrf;
  }
  const response=await fetch(`/api/v1${path}`,{method,headers,credentials:'same-origin',...(body!==undefined?{body:JSON.stringify(body)}:{})});
  const payload=await response.json().catch(()=>({error:{code:'invalid_response',message:'The server returned an unreadable response.'}}));
  if(!response.ok){ const err=payload.error??{}; if(response.status===401||(response.status===403&&err.code==='not_authenticated')){resetApiSession();window.dispatchEvent(new CustomEvent('api-unauthorized',{detail:401}));} throw new ApiError(response.status,err.code??'request_failed',err.message??'The request could not be completed.',err.details); }
  return payload.data as T;
}
export const api={
  get:<T>(path:string)=>request<T>(path),
  post:<T>(path:string,body?:unknown)=>request<T>(path,'POST',body??{}),
  patch:<T>(path:string,body:unknown)=>request<T>(path,'PATCH',body),
  delete:<T>(path:string,body:unknown)=>request<T>(path,'DELETE',body),
  download:async(path:string):Promise<Blob>=>{
    // DRF negotiates JSON renderers before these actions return raw PDF responses.
    // Keep PDF preference while offering JSON as a negotiation fallback.
    const response=await fetch(`/api/v1${path}`,{credentials:'same-origin',headers:{Accept:'application/pdf, application/json'}});
    if(!response.ok){const payload=await response.json().catch(()=>({error:{}}));const error=payload.error??{};if(response.status===401||(response.status===403&&error.code==='not_authenticated')){resetApiSession();window.dispatchEvent(new CustomEvent('api-unauthorized',{detail:401}))}throw new ApiError(response.status,error.code??'request_failed',error.message??'The download could not be completed.',error.details)}
    return response.blob();
  },
  upload:async<T>(path:string,files:File[],onProgress:(percent:number)=>void):Promise<T>=>{
    const token=(await api.csrf()).csrf_token;
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();xhr.open('POST',`/api/v1${path}`);xhr.withCredentials=true;xhr.setRequestHeader('Accept','application/json');xhr.setRequestHeader('X-CSRFToken',token);
      xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(Math.round(e.loaded/e.total*100))};
      xhr.onerror=()=>reject(new ApiError(0,'network_error','The upload could not reach the clinic service.'));
      xhr.onload=()=>{let payload:any;try{payload=JSON.parse(xhr.responseText)}catch{reject(new ApiError(xhr.status,'invalid_response','The server returned an unreadable response.'));return}
        if(xhr.status>=200&&xhr.status<300){resolve(payload.data as T);return}
        const error=payload.error??{};if(xhr.status===401||(xhr.status===403&&error.code==='not_authenticated')){resetApiSession();window.dispatchEvent(new CustomEvent('api-unauthorized',{detail:401}))}
        reject(new ApiError(xhr.status,error.code??'request_failed',error.message??'The upload could not be completed.',error.details));
      };
      const body=new FormData();files.forEach(file=>body.append('files',file,file.name));xhr.send(body);
    });
  },
  csrf:async()=>{const value=await request<{csrf_token:string}>('/auth/csrf/');rememberCsrf(value.csrf_token);return value},
  login:async(username:string,password:string)=>{await api.csrf();const value=await api.post<{user_id:number;csrf_token:string}>('/auth/login/',{username,password});rememberCsrf(value.csrf_token);return value},
  changePassword:async(current_password:string,new_password:string)=>{const value=await api.post<{password_changed:boolean;csrf_token:string}>('/auth/password/change/',{current_password,new_password});rememberCsrf(value.csrf_token);return value},
  logout:()=>api.post<{logged_out:boolean}>('/auth/logout/'),
  me:()=>api.get<SessionInfo>('/auth/me/'),
  clinics:()=>api.get<Membership[]>('/clinics/'),
  activate:(clinic_id:string)=>api.post<{clinic:Clinic;roles:Role[]}>('/clinics/active/',{clinic_id}),
};
