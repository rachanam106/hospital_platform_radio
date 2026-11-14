import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import BillingWorkspace from './BillingWorkspace';
import {api} from './api/client';

vi.mock('./api/client',()=>({api:{get:vi.fn(),post:vi.fn(),patch:vi.fn(),download:vi.fn()}}));
const patient={id:'patient-10000000',given_name:'Mira',family_name:'Synthetic',medical_record_number:'SYN-100'};
const charge={id:'charge-10000000',code:'CT-1',name:'CT scan',category:'ct',unit_price_minor:10000,currency:'INR',tax_rate_bps:500,is_active:true};
const invoice={id:'invoice-10000000',invoice_number:'ABCDEF12-2026-000001',patient_id:patient.id,patient_context:{name:'Mira Synthetic',medical_record_number:'SYN-100'},invoice_date:'2026-01-01',line_items:[{id:'line-1',description:'CT scan',quantity:1,unit_price_minor:10000,tax_rate_bps:500,subtotal_minor:10000,discount_minor:0,tax_minor:500}],gross_amount_minor:10000,discount_minor:0,tax_minor:500,amount_minor:10500,paid_minor:0,balance_minor:10500,currency:'INR',status:'issued'};
const summary={date:'2026-01-01',collections_minor:0,payment_methods:[],outstanding_minor:10500,outstanding_invoice_count:1,partially_paid_count:0,recent_payments:[]};

describe('BillingWorkspace',()=>{
 afterEach(()=>vi.resetAllMocks());
 it('creates an invoice from clinic pricing without submitting client totals',async()=>{
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/invoices/')return{count:0,next:null,previous:null,results:[]} as never;
   if(path==='/charge-items/')return[charge] as never;
   if(path.startsWith('/billing/summary/'))return summary as never;
   if(path.startsWith('/patients/?search='))return{count:1,next:null,previous:null,results:[patient]} as never;
   if(path.startsWith('/examinations/?patient_id='))return[] as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockResolvedValue(invoice as never);
  const notify=vi.fn();render(<BillingWorkspace roles={['receptionist']} notify={notify}/>);
  fireEvent.click(await screen.findByRole('button',{name:/Create invoice/}));
  fireEvent.change(screen.getByPlaceholderText('Search name, MRN or phone'),{target:{value:'SYN-100'}});
  await screen.findByRole('option',{name:/Synthetic, Mira/});
  fireEvent.change(await screen.findByLabelText('Patient'),{target:{value:patient.id}});
  fireEvent.change(screen.getByLabelText('Charge 1'),{target:{value:charge.id}});
  fireEvent.click(screen.getByRole('button',{name:'Issue invoice'}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledWith('/invoices/',{
   patient_id:patient.id,line_items:[{charge_item_id:charge.id,quantity:1}],
  }));
  expect((api.post as ReturnType<typeof vi.fn>).mock.calls[0][1]).not.toHaveProperty('amount_minor');
 });
 it('loads invoice payment history and exposes a receipt after recording',async()=>{
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path==='/invoices/')return{count:1,next:null,previous:null,results:[invoice]} as never;
   if(path==='/charge-items/')return[charge] as never;
   if(path.startsWith('/billing/summary/'))return summary as never;
   if(path===`/invoices/${invoice.id}/`)return invoice as never;
   if(path===`/invoices/${invoice.id}/payments/`)return[] as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  let recorded=false;
  const payment={id:'payment-1',amount_minor:2500,currency:'INR',method:'upi',created_at:'2026-01-01T00:00:00Z',confirmed_received:true,refunded_minor:0,refunds:[]};
  vi.mocked(api.get).mockImplementation(async(path:string)=>{
   if(path===`/invoices/${invoice.id}/payments/`)return(recorded?[payment]:[]) as never;
   if(path===`/invoices/${invoice.id}/`)return invoice as never;
   if(path==='/invoices/')return{count:1,next:null,previous:null,results:[invoice]} as never;
   if(path==='/charge-items/')return[charge] as never;
   if(path.startsWith('/billing/summary/'))return summary as never;
   throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async()=>{recorded=true;return payment as never});
  const notify=vi.fn();render(<BillingWorkspace roles={['receptionist']} notify={notify}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Details'}));
  fireEvent.click(await screen.findByRole('button',{name:'Record payment'}));
  fireEvent.change(screen.getByLabelText('Amount'),{target:{value:'25.00'}});
  fireEvent.change(screen.getByLabelText('Method'),{target:{value:'upi'}});
  fireEvent.click(screen.getByLabelText(/manually confirmed/));
  fireEvent.click(screen.getByRole('button',{name:'Record receipt'}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledWith(`/invoices/${invoice.id}/payments/`,expect.objectContaining({amount_minor:2500,method:'upi',confirmed_received:true,idempotency_key:expect.any(String)})));
  expect(await screen.findByRole('button',{name:'Receipt'})).toBeInTheDocument();
 });
});
