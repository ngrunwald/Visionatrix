import{y as N,z as m,bf as D,_ as M,f as S,l as V,A as G,B as O,C as h,o as r,c as $,H as b,h as y,J as c,i as g,F as E,r as J,V as H,W as R,w as X,I as P,aN as K}from"./Cttk83qL.js";const Q={base:"",background:"bg-white dark:bg-gray-900",divide:"divide-y divide-gray-200 dark:divide-gray-800",ring:"ring-1 ring-gray-200 dark:ring-gray-800",rounded:"rounded-lg",shadow:"shadow",body:{base:"",background:"",padding:"px-4 py-5 sm:p-6"},header:{base:"",background:"",padding:"px-4 py-5 sm:px-6"},footer:{base:"",background:"",padding:"px-4 py-4 sm:px-6"}},T={wrapper:"flex items-center -space-x-px",base:"",rounded:"first:rounded-s-md last:rounded-e-md",default:{size:"sm",activeButton:{color:"primary"},inactiveButton:{color:"white"},firstButton:{color:"white",class:"rtl:[&_span:first-child]:rotate-180",icon:"i-heroicons-chevron-double-left-20-solid"},lastButton:{color:"white",class:"rtl:[&_span:last-child]:rotate-180",icon:"i-heroicons-chevron-double-right-20-solid"},prevButton:{color:"white",class:"rtl:[&_span:first-child]:rotate-180",icon:"i-heroicons-chevron-left-20-solid"},nextButton:{color:"white",class:"rtl:[&_span:last-child]:rotate-180",icon:"i-heroicons-chevron-right-20-solid"}}},d=N(m.ui.strategy,m.ui.pagination,T),Y=N(m.ui.strategy,m.ui.button,D),Z=S({components:{UButton:V},inheritAttrs:!1,props:{modelValue:{type:Number,required:!0},pageCount:{type:Number,default:10},total:{type:Number,required:!0},max:{type:Number,default:7,validate(e){return e>=5&&e<Number.MAX_VALUE}},disabled:{type:Boolean,default:!1},size:{type:String,default:()=>d.default.size,validator(e){return Object.keys(Y.size).includes(e)}},to:{type:Function,default:null},activeButton:{type:Object,default:()=>d.default.activeButton},inactiveButton:{type:Object,default:()=>d.default.inactiveButton},showFirst:{type:Boolean,default:!1},showLast:{type:Boolean,default:!1},firstButton:{type:Object,default:()=>d.default.firstButton},lastButton:{type:Object,default:()=>d.default.lastButton},prevButton:{type:Object,default:()=>d.default.prevButton},nextButton:{type:Object,default:()=>d.default.nextButton},divider:{type:String,default:"…"},class:{type:[String,Object,Array],default:()=>""},ui:{type:Object,default:()=>({})}},emits:["update:modelValue"],setup(e,{emit:n}){const{ui:k,attrs:B}=G("pagination",O(e,"ui"),d,O(e,"class")),o=h({get(){return e.modelValue},set(s){n("update:modelValue",s)}}),v=h(()=>Array.from({length:Math.ceil(e.total/e.pageCount)},(s,l)=>l+1)),f=h(()=>{const s=v.value.length,l=o.value,F=Math.max(e.max,5),u=Math.floor((Math.min(F,s)-5)/2),j=l-u,w=l+u,L=j-1>1,A=w+1<s,a=[];if(s<=F){for(let i=1;i<=s;i++)a.push(i);return a}if(a.push(1),L&&a.push(e.divider),!A){const i=l+u+2-s;for(let p=l-u-i;p<=l-u-1;p++)a.push(p)}for(let i=Math.max(2,j);i<=Math.min(s,w);i++)a.push(i);if(!L){const i=1-(l-u-2);for(let p=l+u+1;p<=l+u+i;p++)a.push(p)}return A&&a.push(e.divider),w<s&&a.push(s),a.length>=3&&a[1]===e.divider&&a[2]===3&&(a[1]=2),a.length>=3&&a[a.length-2]===e.divider&&a[a.length-1]===a.length&&(a[a.length-2]=a.length-1),a}),t=h(()=>o.value>1),C=h(()=>o.value<v.value.length);function z(){t.value&&(o.value=1)}function I(){C.value&&(o.value=v.value.length)}function U(s){typeof s!="string"&&(o.value=s)}function W(){t.value&&o.value--}function q(){C.value&&o.value++}return{ui:k,attrs:B,currentPage:o,pages:v,displayedPages:f,canGoLastOrNext:C,canGoFirstOrPrev:t,onClickPrev:W,onClickNext:q,onClickPage:U,onClickFirst:z,onClickLast:I}}});function _(e,n,k,B,o,v){const f=V;return r(),$("div",c({class:e.ui.wrapper},e.attrs),[b(e.$slots,"first",{onClick:e.onClickFirst},()=>{var t;return[e.firstButton&&e.showFirst?(r(),y(f,c({key:0,size:e.size,to:(t=e.to)==null?void 0:t.call(e,1),disabled:!e.canGoFirstOrPrev||e.disabled,class:[e.ui.base,e.ui.rounded]},{...e.ui.default.firstButton||{},...e.firstButton},{ui:{rounded:""},"aria-label":"First",onClick:e.onClickFirst}),null,16,["size","to","disabled","class","onClick"])):g("",!0)]}),b(e.$slots,"prev",{onClick:e.onClickPrev},()=>{var t;return[e.prevButton?(r(),y(f,c({key:0,size:e.size,to:(t=e.to)==null?void 0:t.call(e,e.currentPage-1),disabled:!e.canGoFirstOrPrev||e.disabled,class:[e.ui.base,e.ui.rounded]},{...e.ui.default.prevButton||{},...e.prevButton},{ui:{rounded:""},"aria-label":"Prev",onClick:e.onClickPrev}),null,16,["size","to","disabled","class","onClick"])):g("",!0)]}),(r(!0),$(E,null,J(e.displayedPages,(t,C)=>{var z;return r(),y(f,c({key:`${t}-${C}`,to:typeof t=="number"?(z=e.to)==null?void 0:z.call(e,t):null,size:e.size,disabled:e.disabled,label:`${t}`,ref_for:!0},t===e.currentPage?{...e.ui.default.activeButton||{},...e.activeButton}:{...e.ui.default.inactiveButton||{},...e.inactiveButton},{class:[{"pointer-events-none":typeof t=="string","z-[1]":t===e.currentPage},e.ui.base,e.ui.rounded],ui:{rounded:""},onClick:()=>e.onClickPage(t)}),null,16,["to","size","disabled","label","class","onClick"])}),128)),b(e.$slots,"next",{onClick:e.onClickNext},()=>{var t;return[e.nextButton?(r(),y(f,c({key:0,size:e.size,to:(t=e.to)==null?void 0:t.call(e,e.currentPage+1),disabled:!e.canGoLastOrNext||e.disabled,class:[e.ui.base,e.ui.rounded]},{...e.ui.default.nextButton||{},...e.nextButton},{ui:{rounded:""},"aria-label":"Next",onClick:e.onClickNext}),null,16,["size","to","disabled","class","onClick"])):g("",!0)]}),b(e.$slots,"last",{onClick:e.onClickLast},()=>{var t;return[e.lastButton&&e.showLast?(r(),y(f,c({key:0,size:e.size,to:(t=e.to)==null?void 0:t.call(e,e.pages.length),disabled:!e.canGoLastOrNext||e.disabled,class:[e.ui.base,e.ui.rounded]},{...e.ui.default.lastButton||{},...e.lastButton},{ui:{rounded:""},"aria-label":"Last",onClick:e.onClickLast}),null,16,["size","to","disabled","class","onClick"])):g("",!0)]})],16)}const se=M(Z,[["render",_]]),x=N(m.ui.strategy,m.ui.card,Q),ee=S({inheritAttrs:!1,props:{as:{type:String,default:"div"},class:{type:[String,Object,Array],default:()=>""},ui:{type:Object,default:()=>({})}},setup(e){const{ui:n,attrs:k}=G("card",O(e,"ui"),x),B=h(()=>H(R(n.value.base,n.value.rounded,n.value.divide,n.value.ring,n.value.shadow,n.value.background),e.class));return{ui:n,attrs:k,cardClass:B}}});function te(e,n,k,B,o,v){return r(),y(K(e.$attrs.onSubmit?"form":e.as),c({class:e.cardClass},e.attrs),{default:X(()=>[e.$slots.header?(r(),$("div",{key:0,class:P([e.ui.header.base,e.ui.header.padding,e.ui.header.background])},[b(e.$slots,"header")],2)):g("",!0),e.$slots.default?(r(),$("div",{key:1,class:P([e.ui.body.base,e.ui.body.padding,e.ui.body.background])},[b(e.$slots,"default")],2)):g("",!0),e.$slots.footer?(r(),$("div",{key:2,class:P([e.ui.footer.base,e.ui.footer.padding,e.ui.footer.background])},[b(e.$slots,"footer")],2)):g("",!0)]),_:3},16,["class"])}const ne=M(ee,[["render",te]]);export{ne as _,se as a};
