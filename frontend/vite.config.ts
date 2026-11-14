import { defineConfig } from 'vitest/config';
import { loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({mode})=>{const env=loadEnv(mode,'.');return{plugins:[react()],server:{proxy:{'/api':{target:'http://127.0.0.1:8000',changeOrigin:false},'/ohif':{target:env.OHIF_DEV_SERVER||'http://127.0.0.1:3000',changeOrigin:false,rewrite:path=>path.replace(/^\/ohif/,'')}}},test:{environment:'jsdom',globals:true,setupFiles:'./src/test-setup.ts'}}});
