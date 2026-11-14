import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {ClinicalHistoryPanel} from './App';
import {api} from './api/client';

vi.mock('./api/client',()=>({api:{get:vi.fn(),post:vi.fn(),patch:vi.fn(),download:vi.fn(),delete:vi.fn()}}));

describe('ClinicalHistoryPanel',()=>{
 afterEach(()=>vi.resetAllMocks());
 it('keeps source text and extraction separate until a clinician confirms corrected candidates',async()=>{
  const pending={id:'entry-1',patient_id:'patient-1',original_text:'Patient reports cough for three days.',extracted_mentions:[{text:'cough',normalized:'cough',start:15,end:20,assertion:'present',duration_text:'for three days',confidence:null}],confirmed_mentions:null,extractor_name:'rule-based-symptom-extractor',extractor_version:'1.0.0',status:'pending_review',created_at:'2026-02-01T00:00:00Z'};
  vi.mocked(api.get).mockResolvedValue([] as never);
  vi.mocked(api.post).mockImplementation(async(path:string,body:unknown)=>path.endsWith('/review/')?{...pending,...(body as object),status:'confirmed',reviewed_by_id:7,confirmed_mentions:(body as {confirmed_mentions:unknown[]}).confirmed_mentions} as never:pending as never);
  const notify=vi.fn();render(<ClinicalHistoryPanel patientId="patient-1" notify={notify}/>);
  fireEvent.change(await screen.findByLabelText('Clinician-entered history'),{target:{value:pending.original_text}});
  fireEvent.click(screen.getByRole('button',{name:'Extract symptom candidates'}));
  await screen.findByRole('heading',{name:/Extracted candidates/});
  expect(screen.getByText(pending.original_text)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Normalized symptom 1'),{target:{value:'dry cough'}});
  fireEvent.click(screen.getByRole('button',{name:'Confirm reviewed history'}));
  await waitFor(()=>expect(api.post).toHaveBeenLastCalledWith('/patients/patient-1/clinical-history/entries/entry-1/review/',expect.objectContaining({decision:'confirmed',confirmed_mentions:[expect.objectContaining({normalized:'dry cough'})]})));
  expect(await screen.findByText(/dry cough · present/)).toBeInTheDocument();
  expect(notify).toHaveBeenCalledWith('ok','Clinical history confirmed and appended.');
 });
});
