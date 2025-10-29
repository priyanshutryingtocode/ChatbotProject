from langchain_core.prompts import ChatPromptTemplate
from setup import chatmodel, SYSTEM_PROMPT
from query import query_database, format_database_context

class OrderChatHandler:
    def __init__(self):
        self.llm = chatmodel()
        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
        
    def process_user_message(self, user_input):
        
        if not user_input or not user_input.strip():
            return "Hello! I'm here to help you check your order status. Please provide your order ID, email, phone number, or name.", {}
        
        try:
       
            self.conversation_history.append(("human", user_input))
            
          
            db_results = query_database(user_input)
            
            
            if not db_results and self.current_order_context:
                db_results = self.last_db_results
                db_context = self.current_order_context
            elif db_results:
                
                db_context = format_database_context(db_results)
                self.current_order_context = db_context
                self.last_db_results = db_results
            else:
                db_context = None
            
            
            if db_context:
                system_prompt = f"{SYSTEM_PROMPT}\n\n{db_context}\n\nPlease use the above order information to provide accurate and helpful responses. Maintain context from previous messages in this conversation."
            else:
                system_prompt = SYSTEM_PROMPT
            
            
            messages = [("system", system_prompt)]
            
            
            recent_history = self.conversation_history[-6:]
            messages.extend(recent_history)
            
            
            if not recent_history or recent_history[-1][1] != user_input:
                messages.append(("human", user_input))
            
            prompt_template = ChatPromptTemplate.from_messages(messages)
            
            chain = prompt_template | self.llm
            response = chain.invoke({})
            
            
            self.conversation_history.append(("assistant", response.content))
            
            return response.content, db_results or self.last_db_results
            
        except Exception as e:
            error_message = "I'm sorry, there was an error processing your request. Please try again or contact support."
            self.conversation_history.append(("assistant", error_message))
            return error_message, {}
    
    def clear_context(self):
        
        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
    
    def get_conversation_history(self):
        
        return self.conversation_history.copy()
    
    def has_order_context(self):
        
        return self.current_order_context is not None
    
    def get_current_order_info(self):
       
        return self.last_db_results if self.last_db_results else None
