"""Create a standalone English HTML report and ZIP from any completed analysis run."""
from __future__ import annotations
import argparse
import base64
from datetime import date
import json
from pathlib import Path
import zipfile
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent

def export_run(run_dir,output_dir=None):
    run=Path(run_dir).resolve()
    destination=Path(output_dir).resolve() if output_dir else run
    destination.mkdir(parents=True,exist_ok=True)
    cfg=json.loads((run/'run_config.json').read_text(encoding='utf-8'))
    asof=date.fromisoformat(str(cfg['positions_asof'])).isoformat()
    soup=BeautifulSoup((run/'report.html').read_text(encoding='utf-8'),'html.parser')
    if not soup.html or soup.html.get('lang')!='en':
        raise ValueError('Expected a completed English report; run the current main.py first')
    for tag in soup.find_all('img'):
        src=tag.get('src','')
        if src.startswith('data:image/'): continue
        file=(run/src).resolve()
        if not file.is_relative_to(run) or not file.is_file() or file.suffix.lower()!='.png':
            raise ValueError(f'Report image must be an existing PNG inside the run directory: {src}')
        tag['src']='data:image/png;base64,'+base64.b64encode(file.read_bytes()).decode('ascii')
    standalone=destination/f'Portfolio_Report_EN_{asof}.html'
    standalone.write_text(str(soup),encoding='utf-8')
    (destination/'report_en.html').write_text(str(soup),encoding='utf-8')
    bundle=destination/f'Portfolio_Report_EN_{asof}.zip'
    with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.write(standalone,standalone.name)
        for file in sorted(run.iterdir()):
            if file.is_file() and file.suffix in ('.csv','.json'):
                z.write(file,'supporting_data/'+file.name)
        z.writestr('READ_ME.txt',f'Valuation date: {asof}\nOpen {standalone.name} in a browser. All charts are embedded; no external image files are required. Supporting CSV/JSON files are included for audit. Historical results are not forecasts.\n')
    return standalone,bundle

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,help='Completed run directory; defaults to the latest run from the local config')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--output-dir',type=Path,help='Optional download destination')
    args=parser.parse_args()
    run=args.run
    if run is None:
        cfg=json.loads(args.config.read_text(encoding='utf-8-sig'))
        run=Path((ROOT/cfg['output_dir']/'LATEST.txt').read_text(encoding='utf-8').strip())
    report,bundle=export_run(run,args.output_dir)
    print(f'Standalone English report: {report}\nDownload bundle: {bundle}')

if __name__=='__main__': main()
