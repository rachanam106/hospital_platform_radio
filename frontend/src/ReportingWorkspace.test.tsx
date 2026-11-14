import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import ReportingWorkspace from './ReportingWorkspace';
import {api} from './api/client';

vi.mock('./api/client',()=>({api:{get:vi.fn(),post:vi.fn(),patch:vi.fn(),download:vi.fn()}}));

const exam={id:'exam-10000000',patient_id:'patient-10000000',modality:'CT',procedure_name:'CT chest',clinical_indication:'Synthetic test indication',status:'acquired',created_at:'2026-01-01T00:00:00Z'};
const templateRows=[{key:'ct',label:'CT',modalities:['CT'],fields:['technique','comparison','findings','impression','additional_comments'],sections:['Technique','Comparison','Findings','Impression','Additional comments']},{key:'general',label:'General radiology',modalities:[],fields:['technique','comparison','findings','impression','additional_comments'],sections:[]}];
const draft={id:'report-10000000',examination_id:exam.id,version:1,template:'ct',technique:'',sequences:'',comparison:'',findings:'',impression:'',additional_comments:'',status:'draft',updated_at:'2026-01-01T00:00:00.000Z'};

describe('ReportingWorkspace',()=>{
 afterEach(()=>vi.resetAllMocks());
 it('loads assigned context, selects modality template, autosaves structured fields and signs',async()=>{
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/examinations/')return{count:1,results:[exam]} as never;
   if(path==='/reports/')return{count:0,results:[]} as never;
   if(path==='/reports/templates/')return templateRows as never;
   if(path.includes('/ai-assists/'))return[] as never;
   if(path.includes('/clinical-history/'))return{examinations:[]} as never;
   if(path.includes('/imaging-studies/'))return[] as never;
   if(path.includes('/patients/'))return{given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'} as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async(path:string,body?:unknown)=>path==='/reports/'?draft as never:{...draft,...(body as object),status:'signed',signed_at:'2026-01-02T00:00:00Z'} as never);
  vi.mocked(api.patch).mockImplementation(async(_path:string,body:unknown)=>({...draft,...(body as object)}) as never);
  const notify=vi.fn();render(<ReportingWorkspace notify={notify}/>);
  fireEvent.click(await screen.findByText('CT chest'));
  await screen.findByText(/SYN-100/);
  expect(screen.getByLabelText('Report structure')).toHaveValue('ct');
  fireEvent.click(screen.getByRole('button',{name:'Start report'}));
  await screen.findByRole('heading',{name:/CT chest/});
  expect(screen.getByLabelText('Comparison')).toBeInTheDocument();
  await waitFor(()=>expect(api.post).toHaveBeenCalledWith('/reports/',{examination_id:exam.id,template:'ct'}));
  fireEvent.change(screen.getByLabelText('Findings'),{target:{value:'Synthetic verified findings'}});
  fireEvent.change(screen.getByLabelText('Impression'),{target:{value:'Synthetic impression'}});
  await waitFor(()=>expect(api.patch).toHaveBeenCalledWith('/reports/report-10000000/',expect.objectContaining({findings:'Synthetic verified findings',impression:'Synthetic impression'})),{timeout:3000});
  fireEvent.click(screen.getByRole('button',{name:/Sign version/}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledWith('/reports/report-10000000/sign/',{expected_updated_at:'2026-01-01T00:00:00.000Z'}));
  expect(notify).toHaveBeenCalledWith('ok','Report version signed and locked.');
 });

 it('restores a saved draft when reopening an examination',async()=>{
  const saved={...draft,technique:'Saved synthetic technique',comparison:'Prior synthetic study',findings:'Saved synthetic finding',impression:'Saved synthetic impression'};
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/examinations/')return{count:1,results:[exam]} as never;
   if(path==='/reports/')return{count:1,results:[saved]} as never;
   if(path==='/reports/templates/')return templateRows as never;
   if(path.includes('/ai-assists/'))return[] as never;
   if(path.includes('/clinical-history/'))return{examinations:[]} as never;
   if(path.includes('/imaging-studies/'))return[] as never;
   if(path.includes('/patients/'))return{given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'} as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockResolvedValue(saved as never);
  const notify=vi.fn();render(<ReportingWorkspace notify={notify}/>);
  fireEvent.click(await screen.findByText('CT chest'));
  fireEvent.click(await screen.findByRole('button',{name:'Open draft'}));
  expect(await screen.findByLabelText('Findings')).toHaveValue('Saved synthetic finding');
  expect(screen.getByLabelText('Impression')).toHaveValue('Saved synthetic impression');
  expect(api.post).not.toHaveBeenCalled();
 });

 it('does not offer release for an earlier signed version when an amendment draft exists',async()=>{
  const oldSigned={...draft,id:'report-old',version:1,status:'signed',findings:'Signed v1 finding',impression:'Signed v1 impression',signed_context:{patient:{name:'Mira Synthetic'}}};
  const amendment={...draft,id:'report-new',version:2,status:'draft',amends_id:'report-old'};
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/examinations/')return{count:1,results:[exam]} as never;
   if(path==='/reports/')return{count:2,results:[amendment,oldSigned]} as never;
   if(path==='/reports/templates/')return templateRows as never;
   if(path.includes('/ai-assists/'))return[] as never;
   if(path.includes('/clinical-history/'))return{examinations:[]} as never;
   if(path.includes('/imaging-studies/'))return[] as never;
   if(path.includes('/patients/'))return{given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'} as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  const notify=vi.fn();render(<ReportingWorkspace notify={notify}/>);
  fireEvent.click(await screen.findByText('CT chest'));
  fireEvent.click(await screen.findByRole('button',{name:'Version 1'}));
  expect(await screen.findByText(/An earlier signed version cannot be released/)).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Release report'})).not.toBeInTheDocument();
  expect(screen.getByRole('button',{name:/Signed PDF · v1/})).toBeInTheDocument();
 });

 it('flushes pending draft text before switching examinations',async()=>{
  const secondExam={...exam,id:'exam-20000000',patient_id:'patient-20000000',modality:'XR',procedure_name:'XR chest'};
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/examinations/')return{count:2,results:[exam,secondExam]} as never;
   if(path==='/reports/')return{count:0,results:[]} as never;
   if(path==='/reports/templates/')return templateRows as never;
   if(path.includes('/ai-assists/'))return[] as never;
   if(path.includes('/clinical-history/'))return{examinations:[]} as never;
   if(path.includes('/imaging-studies/'))return[] as never;
   if(path.includes('/patients/'))return{given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'} as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async(path:string)=>path==='/reports/'?draft as never:draft as never);
  vi.mocked(api.patch).mockImplementation(async(_path:string,body:unknown)=>({...draft,...(body as object)}) as never);
  const notify=vi.fn();render(<ReportingWorkspace notify={notify}/>);
  fireEvent.click(await screen.findByText('CT chest'));
  fireEvent.click(screen.getByRole('button',{name:'Start report'}));
  fireEvent.change(await screen.findByLabelText('Findings'),{target:{value:'Pending synthetic text'}});
  fireEvent.click(screen.getByText('XR chest'));
  await waitFor(()=>expect(api.patch).toHaveBeenCalledWith('/reports/report-10000000/',expect.objectContaining({findings:'Pending synthetic text'})));
  await screen.findByRole('heading',{name:/XR chest/});
 });

 it('keeps X-ray evidence structuring separate until radiologist accepts it for draft editing',async()=>{
  const xrayExam={...exam,modality:'XR',procedure_name:'XR chest'};
  const xrayDraft={...draft,template:'xray'};
  const xrayTemplates=[{key:'xray',label:'X-ray',modalities:['XR'],fields:['technique','findings','impression','additional_comments'],sections:['Technique','Findings','Impression','Additional comments']},...templateRows];
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/examinations/')return{count:1,results:[xrayExam]} as never;
   if(path==='/reports/')return{count:0,results:[]} as never;
   if(path==='/reports/templates/')return xrayTemplates as never;
   if(path.includes('/ai-assists/'))return[] as never;
   if(path.includes('/clinical-history/'))return{examinations:[]} as never;
   if(path.includes('/imaging-studies/'))return[] as never;
   if(path.includes('/patients/'))return{given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'} as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async(path:string,body?:unknown)=>{
   if(path==='/reports/')return xrayDraft as never;
   if(path.endsWith('/ai-assist/'))return{id:'assist-1',model_name:'rule-based-evidence-structurer',model_version:'1.0.0',created_at:'2026-01-01T00:00:00Z',review_status:'pending_review',generated_sections:{findings:'lungs — positive: Small left pleural effusion.',technique:'',impression:''},verification:{claims:[{claim:'Small left pleural effusion.',status:'supported',reason:'Close evidence match.',evidence_index:0}],limitations:['Text only.']}} as never;
   if(path.endsWith('/review-ai-assist/'))return{id:'assist-1',review_status:(body as {decision:string}).decision,generated_sections:{findings:'lungs — positive: Small left pleural effusion.',technique:'',impression:''},verification:{claims:[],limitations:[]}} as never;
   return{...xrayDraft,status:'signed'} as never;
  });
  vi.mocked(api.patch).mockImplementation(async(_path:string,body:unknown)=>({...xrayDraft,...(body as object)}) as never);
  const notify=vi.fn();render(<ReportingWorkspace notify={notify}/>);
  fireEvent.click(await screen.findByText('XR chest'));
  fireEvent.click(await screen.findByRole('button',{name:'Start report'}));
  await screen.findByRole('heading',{name:/XR chest/});
  fireEvent.change(screen.getByLabelText('Anatomy'),{target:{value:'lungs'}});
  fireEvent.change(screen.getByLabelText('Observed finding'),{target:{value:'Small left pleural effusion.'}});
  fireEvent.click(screen.getByRole('button',{name:'Structure supplied evidence'}));
  await screen.findByText('Structured suggestion · unverified');
  expect(api.patch).not.toHaveBeenCalledWith(expect.stringContaining('/reports/'),expect.objectContaining({findings:expect.stringContaining('Small left')}));
  fireEvent.click(screen.getByRole('button',{name:'Accept for editing'}));
  await waitFor(()=>expect(api.patch).toHaveBeenCalledWith(expect.stringContaining('/reports/'),expect.objectContaining({findings:expect.stringContaining('Small left')})));
  expect((screen.getByLabelText('Findings') as HTMLTextAreaElement).value).toContain('Small left');
 });
});
